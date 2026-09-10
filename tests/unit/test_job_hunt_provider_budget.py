"""The deployment's daily provider budget, which is not anybody's subscription.

Jobvetta grants 50 requests per API key per UTC day, shared by every student on the deployment.
That ceiling belongs to the key. A premium subscription buys unlimited Job Hunt and is still
subject to it, and the two refusals must never be able to answer for each other:

    402  your free search is used            -> buying premium fixes it
    429  the day's provider budget is gone   -> buying premium does not

These tests cover the counter itself and the fact that the search path consults it. The
concurrency ones use real threads on a file-backed database for the same reason the entitlement
ones do: an "increment if there is room" that is actually a read followed by a write passes
every sequential test and fails the only case it exists for.
"""

import datetime
import threading
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- registers every model with Base.metadata
from app.database.base import Base
from app.models.provider_call_budget import ProviderCallBudget
from app.services.job_hunt import provider_budget
from app.services.job_hunt.providers.base import JobProviderRateLimitError

TODAY = datetime.date(2026, 9, 10)
TOMORROW = datetime.date(2026, 9, 11)


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'budget.db'}", connect_args={"timeout": 30}
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def sessions(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db(sessions):
    session = sessions()
    try:
        yield session
    finally:
        session.close()


class TestAProviderWithNoDocumentedCeilingIsNotMetered:
    def test_none_means_nothing_is_written(self, db):
        """Adzuna and Careerjet publish per-minute rates, not a per-day ceiling. Metering a
        limit nobody imposed would be a restriction this codebase invented, which is the same
        class of mistake as inventing a job."""
        for _ in range(200):
            provider_budget.reserve_call(db, "careerjet", None, today=TODAY)

        assert db.query(ProviderCallBudget).count() == 0

    def test_none_never_refuses(self, db):
        provider_budget.reserve_call(db, "careerjet", None, today=TODAY)  # no exception


class TestTheCounter:
    def test_the_first_call_creates_the_day_s_row(self, db):
        provider_budget.reserve_call(db, "jobvetta", 50, today=TODAY)

        row = db.query(ProviderCallBudget).one()
        assert (row.provider, row.usage_date, row.call_count) == ("jobvetta", TODAY, 1)

    def test_calls_accumulate_on_one_row_per_day(self, db):
        for _ in range(5):
            provider_budget.reserve_call(db, "jobvetta", 50, today=TODAY)

        assert db.query(ProviderCallBudget).count() == 1
        assert provider_budget.calls_used_today(db, "jobvetta", today=TODAY) == 5

    def test_the_whole_budget_is_usable_and_the_next_call_is_refused(self, db):
        for _ in range(50):
            provider_budget.reserve_call(db, "jobvetta", 50, today=TODAY)

        with pytest.raises(JobProviderRateLimitError):
            provider_budget.reserve_call(db, "jobvetta", 50, today=TODAY)

        # The refusal must not also consume a slot, or the count would drift past the ceiling.
        assert provider_budget.calls_used_today(db, "jobvetta", today=TODAY) == 50

    def test_a_new_utc_day_starts_a_new_budget(self, db):
        for _ in range(50):
            provider_budget.reserve_call(db, "jobvetta", 50, today=TODAY)

        provider_budget.reserve_call(db, "jobvetta", 50, today=TOMORROW)

        assert provider_budget.calls_used_today(db, "jobvetta", today=TOMORROW) == 1
        assert provider_budget.calls_used_today(db, "jobvetta", today=TODAY) == 50

    def test_each_provider_has_its_own_budget(self, db):
        """The limit belongs to a key. Switching providers must not inherit the other one's
        count, and running two must not have them spend each other's."""
        for _ in range(50):
            provider_budget.reserve_call(db, "jobvetta", 50, today=TODAY)

        provider_budget.reserve_call(db, "someone_else", 50, today=TODAY)

        assert provider_budget.calls_used_today(db, "someone_else", today=TODAY) == 1

    def test_a_zero_budget_refuses_immediately(self, db):
        """Not a state any real provider is in, but the arithmetic should not treat "no calls
        allowed" as "one call allowed"."""
        with pytest.raises(JobProviderRateLimitError):
            provider_budget.reserve_call(db, "jobvetta", 0, today=TODAY)

    def test_the_refusal_names_the_budget_and_the_day_for_an_operator(self, db):
        """This message is logged and never shown to a student -- the API layer maps
        JobProviderRateLimitError to a fixed sentence -- so it may be specific."""
        with pytest.raises(JobProviderRateLimitError) as caught:
            provider_budget.reserve_call(db, "jobvetta", 0, today=TODAY)

        assert "jobvetta" in str(caught.value)
        assert str(TODAY) in str(caught.value)

    def test_counting_a_day_with_no_calls_is_zero_not_an_error(self, db):
        assert provider_budget.calls_used_today(db, "jobvetta", today=TODAY) == 0


class TestUnderConcurrency:
    def _race(self, sessions, count, budget):
        barrier = threading.Barrier(count)
        outcomes = [None] * count

        def run(index):
            db = sessions()
            try:
                barrier.wait(timeout=30)
                provider_budget.reserve_call(db, "jobvetta", budget, today=TODAY)
                outcomes[index] = "allowed"
            except JobProviderRateLimitError:
                outcomes[index] = "refused"
            except Exception as exc:  # noqa: BLE001
                outcomes[index] = exc
            finally:
                db.close()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return outcomes

    def test_simultaneous_first_calls_do_not_create_two_rows_for_one_day(self, sessions):
        """The row does not exist yet, so every thread tries to create it. Exactly one insert
        can land; the rest have to notice and fall back to incrementing what is now there,
        rather than failing or double-counting."""
        outcomes = self._race(sessions, 8, 50)

        assert all(outcome == "allowed" for outcome in outcomes), outcomes
        db = sessions()
        assert db.query(ProviderCallBudget).count() == 1
        assert provider_budget.calls_used_today(db, "jobvetta", today=TODAY) == 8
        db.close()

    def test_racing_for_the_last_slot_gives_it_to_exactly_one(self, sessions):
        """The case a read-then-write loses: several workers all see 49 and all decide there
        is room. The predicate is inside the UPDATE, so the storage engine decides instead."""
        db = sessions()
        for _ in range(49):
            provider_budget.reserve_call(db, "jobvetta", 50, today=TODAY)
        db.close()

        outcomes = self._race(sessions, 6, 50)

        assert outcomes.count("allowed") == 1, outcomes
        assert outcomes.count("refused") == 5
        db = sessions()
        assert provider_budget.calls_used_today(db, "jobvetta", today=TODAY) == 50
        db.close()

    def test_the_count_never_exceeds_the_ceiling_under_load(self, sessions):
        """The property that actually matters: whatever the interleaving, the key is never
        asked for more than it grants."""
        outcomes = self._race(sessions, 20, 7)

        assert outcomes.count("allowed") == 7, outcomes
        db = sessions()
        assert provider_budget.calls_used_today(db, "jobvetta", today=TODAY) == 7
        db.close()


class TestTheSearchPathConsultsIt:
    """A budget nothing reads is a comment. These drive the real service.search()."""

    @pytest.fixture()
    def seeded(self, sessions):
        from app.models.profile import StudentProfile
        from app.models.user import User
        from app.services.job_hunt import service

        db = sessions()
        user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        db.add(user)
        db.commit()
        profile = StudentProfile(user_id=user.id)
        db.add(profile)
        db.commit()
        search_profile = service.get_or_create_search_profile(db, profile.id)
        search_profile.target_role_title = "Data Engineer"
        db.commit()
        return db, profile

    def test_a_metered_provider_spends_one_slot_per_search(self, seeded):
        from app.services.job_hunt import service

        db, profile = seeded

        service.search(db, profile, BudgetedProvider(), page=1, page_size=10)

        assert provider_budget.calls_used_today(db, "budgeted") == 1

    def test_an_exhausted_budget_stops_the_search_before_the_provider_is_called(self, seeded):
        """Checked before the outbound request, not discovered from a 429 afterwards -- the
        point of a ceiling we already know about is not having to spend a call to learn it."""
        from app.services.job_hunt import service

        db, profile = seeded
        provider = BudgetedProvider()
        for _ in range(provider.daily_call_budget):
            service.search(db, profile, provider, page=1, page_size=10)
        calls_before = provider.calls

        with pytest.raises(JobProviderRateLimitError):
            service.search(db, profile, provider, page=1, page_size=10)

        assert provider.calls == calls_before, "no request may be sent once the budget is gone"

    def test_an_unmetered_provider_is_never_stopped(self, seeded):
        from app.services.job_hunt import service

        db, profile = seeded
        provider = BudgetedProvider(budget=None, name="unmetered")

        for _ in range(12):
            service.search(db, profile, provider, page=1, page_size=10)

        assert provider.calls == 12
        assert db.query(ProviderCallBudget).count() == 0

    def test_a_provider_object_without_the_attribute_is_treated_as_unmetered(self, seeded):
        """The provider arrives through a FastAPI dependency and may be a duck-typed object
        that predates this attribute. Absent must mean "no documented ceiling", not a crash
        halfway through a search."""
        from app.services.job_hunt import service

        db, profile = seeded

        class Antique:
            name = "antique"
            attribution_text = ""
            attribution_url = None
            cache_ttl = datetime.timedelta(hours=6)

            def search(self, query):
                from app.services.job_hunt.providers.base import ProviderSearchResult

                return ProviderSearchResult(jobs=[], total_available=0)

        outcome = service.search(db, profile, Antique(), page=1, page_size=10)

        assert outcome.listings == []


class BudgetedProvider:
    """A provider that declares a small ceiling, so exhausting it takes three calls not fifty."""

    attribution_text = "Jobs by Budgeted"
    attribution_url = None
    cache_ttl = datetime.timedelta(hours=6)

    def __init__(self, budget=3, name="budgeted"):
        self.name = name
        self.daily_call_budget = budget
        self.calls = 0

    def search(self, query):
        from app.services.job_hunt.providers.base import ProviderSearchResult

        self.calls += 1
        return ProviderSearchResult(jobs=[], total_available=0)
