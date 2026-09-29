"""What happens to a Job Hunt write when it loses a race to another request.

Three places insert a row that a concurrent request may already have inserted: listings and
matches during a search, and delivery records during an alert run. All three recover by
catching IntegrityError, and all three used to recover with a bare `db.rollback()`.

That is the bug these tests exist for. Every one of the three runs inside a *loop* whose work
is committed once at the end, so a session-wide rollback does not undo the duplicate -- it
undoes everything the loop has done so far. The visible symptoms differ by site and all three
are silent:

  * a search page that quietly loses most of its jobs;
  * matches computed and thrown away, so scores go missing from a page that renders anyway;
  * deliveries discarded, so already-announced jobs are announced again on the next run --
    the exact duplicate-notification failure the delivery table exists to prevent.

The fix is a SAVEPOINT per insert. These tests pin the behaviour rather than the mechanism:
each asserts that work done before the losing insert survives it.

A file-backed SQLite database is used rather than the shared in-memory one from conftest. That
fixture uses StaticPool, which hands every session the *same* connection, so two "concurrent"
sessions would share one transaction and a commit by either would commit the other's pending
writes -- the test would pass no matter what the code did.
"""

import datetime
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.main  # noqa: F401  -- imports every model so create_all sees the full schema
from app.database.base import Base
from app.models.job_alert import JobAlert, JobAlertDelivery
from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.saved_job import SavedJob
from app.models.user import User
from app.services.job_hunt import alerts, service
from app.services.job_hunt.providers.base import ProviderJob, ProviderSearchResult
from tests.fixtures.premium_helpers import grant_premium

# These tests also run against PostgreSQL when TEST_DATABASE_URL is set; unset, the behaviour
# is exactly as before.
#
# Worth being precise about what that adds, because it is easy to overclaim. Replacing the
# savepoint with the old bare `db.rollback()` fails one of these tests on SQLite *and* on
# PostgreSQL -- the property they assert (work done before the losing insert survives it) is
# broken by a session-wide rollback on any backend, so SQLite alone already protects it.
#
# What PostgreSQL adds is the failure mode SQLite cannot have. There, a failed statement
# poisons the whole transaction until it is rolled back, and every later statement raises
# InFailedSqlTransaction. So a recovery path that merely looks correct on SQLite can still be
# wrong in production, and running these here rules that out rather than assuming it.
_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def db(tmp_path):
    """A real session with a real connection. See the module docstring for why not conftest's
    client, and the note above for why PostgreSQL matters here in particular."""
    if _TEST_DATABASE_URL:
        # NullPool for the same reason conftest uses it: no connection may outlive the test
        # whose tables are about to be dropped. A shared pool would hold locks past teardown.
        engine = create_engine(_TEST_DATABASE_URL, poolclass=NullPool,
                               connect_args={"options": "-c lock_timeout=15000"})
        # A real database persists between tests; the in-memory and tmp_path ones do not.
        Base.metadata.drop_all(bind=engine)
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'jobhunt.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        if _TEST_DATABASE_URL:
            Base.metadata.drop_all(bind=engine)
        engine.dispose()


NOW = datetime.datetime(2026, 9, 9, 12, 0, 0)


class FakeProvider:
    name = "fake"
    attribution_text = "Jobs by Fake"
    attribution_url = None
    cache_ttl = datetime.timedelta(hours=6)

    def search(self, query):  # pragma: no cover -- these tests call the upserts directly
        raise NotImplementedError


class StubProvider(FakeProvider):
    """Returns a fixed set of adverts. Deliberately not a mock: run_alert is meant to be
    exercised through a real search, and a provider that returns adverts is the smallest thing
    that lets it be."""

    def __init__(self, jobs):
        self._jobs = jobs

    def search(self, query):
        return ProviderSearchResult(jobs=list(self._jobs), total_available=len(self._jobs))


def provider_job(job_id: str) -> ProviderJob:
    return ProviderJob(
        provider="fake",
        provider_job_id=job_id,
        url=f"https://jobs.example.com/{job_id}",
        title="Data Analyst",
        company="Example Ltd",
    )


def _blind_once(monkeypatch, function_name, matches):
    """Make one service lookup miss a single time, then behave normally.

    This is the whole simulation, and it is deliberately this small. SQLite serialises
    writers, so a genuine second connection cannot commit while the session under test holds
    an open write transaction -- the race window has to be reproduced rather than raced. What
    is reproduced is exactly it: the lookup ran before the other request committed and saw
    nothing; the INSERT then hit the constraint; the recovery lookup runs afterwards and sees
    the winner.
    """
    real = getattr(service, function_name)
    missed = {"done": False}

    def once(*args):
        if matches(args) and not missed["done"]:
            missed["done"] = True
            return None
        return real(*args)

    monkeypatch.setattr(service, function_name, once)


def make_profile(db, *, subscribed: bool = False) -> StudentProfile:
    """A student, optionally with a real active subscription.

    `subscribed` exists for the alert tests below. Background alert runs require an active
    plan (see alerts.run_alert): a scheduled search that runs on its own would otherwise turn
    a one-search free tier into an unlimited one with a timer attached. Written as a real
    `subscriptions` row rather than a patched predicate, so these tests keep exercising the
    genuine entitlement path while checking what they are actually about -- the delivery loop.
    """
    email = f"{uuid.uuid4().hex}@example.com"
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    if subscribed:
        grant_premium(db, email)
    return profile


class TestListingInsertRace:
    def test_a_lost_insert_race_does_not_discard_the_listings_already_read(self, db, monkeypatch):
        """The first page of a search survives a duplicate part-way down it.

        The race is simulated at its real seam: _find_listing says "not stored", and the
        INSERT then fails because another request stored it in between. That is precisely the
        window the IntegrityError handler exists for, and it is the only way to reach the
        handler deterministically -- SQLite serialises writers, so a genuine second writer
        cannot commit while this session holds an open write transaction.
        """
        provider = FakeProvider()

        # The advert another request has already committed. Present in the database, but
        # invisible to the lookup, exactly as it is during the race window.
        db.add(
            JobListing(
                provider="fake",
                provider_job_id="b",
                url="https://jobs.example.com/b",
                title="Data Analyst",
                fetched_at=NOW,
            )
        )
        db.commit()

        # Misses exactly once for "b", then tells the truth. That is the race window as the
        # code actually experiences it: the pre-insert lookup ran before the other request
        # committed, and the recovery lookup runs after -- so the recovery must find the
        # winner. A stub that always missed would test a different, impossible world.
        _blind_once(monkeypatch, "_find_listing", lambda args: args[2] == "b")

        first = service._upsert_listing(db, provider_job("a"), provider, NOW)
        assert first is not None

        # Loses the race. Returns the winner's row rather than raising.
        second = service._upsert_listing(db, provider_job("b"), provider, NOW)
        assert second is not None
        assert second.provider_job_id == "b"

        third = service._upsert_listing(db, provider_job("c"), provider, NOW)
        assert third is not None

        db.commit()

        stored = {row.provider_job_id for row in db.query(JobListing).all()}
        # Before the fix this was {"b"} -- the rollback destroyed "a", and "c" was never
        # reached in the real loop because the session had already been reset underneath it.
        assert stored == {"a", "b", "c"}

    def test_the_loser_adopts_the_winning_row_rather_than_inserting_a_second_one(
        self, db, monkeypatch
    ):
        provider = FakeProvider()
        db.add(
            JobListing(
                provider="fake",
                provider_job_id="b",
                url="https://jobs.example.com/b",
                title="Data Analyst",
                fetched_at=NOW,
            )
        )
        db.commit()
        winner_id = db.query(JobListing).one().id

        _blind_once(monkeypatch, "_find_listing", lambda args: args[2] == "b")
        adopted = service._upsert_listing(db, provider_job("b"), provider, NOW)
        db.commit()

        assert adopted.id == winner_id
        assert db.query(JobListing).count() == 1


class TestManualJobInsertRace:
    """Two submissions of the same manually entered job, at once.

    Phase 4 reuses the existing (provider, provider_job_id) unique constraint as the
    deduplication key for manual entry: the id is derived from the link, so the constraint
    that already keeps two concurrent searches from storing one advert twice also keeps a
    double-tapped submit button from producing two tracked applications. These pin that it
    genuinely recovers rather than raising into the student's face, and that the recovery path
    is the same savepoint the provider path uses.
    """

    def test_a_double_submit_of_the_same_link_produces_one_tracked_job(self, db, monkeypatch):
        profile = make_profile(db)
        url = "https://careers.example.com/de-2026"

        first = service.add_manual_job(db, profile.id, title="Data Engineer", url=url)

        # The second submission's pre-insert lookup runs before the first committed, so it
        # sees nothing and tries to insert -- which is the race window as the code experiences
        # it. The recovery lookup afterwards must find the winner.
        _blind_once(monkeypatch, "_find_listing", lambda args: args[1] == "manual")
        second = service.add_manual_job(db, profile.id, title="Data Engineer", url=url)

        assert second.id == first.id, "the loser adopts the winning row, it does not raise"
        assert db.query(JobListing).filter(JobListing.provider == "manual").count() == 1
        assert db.query(SavedJob).filter(SavedJob.profile_id == profile.id).count() == 1

    def test_losing_the_race_leaves_the_session_usable(self, db, monkeypatch):
        """The failure the savepoint exists to prevent, at this call site.

        Each add_manual_job commits, so unlike the search loop there is rarely pending work for
        a bare rollback to destroy. What a bare rollback would still do is leave the session in
        a state the rest of this call cannot use -- on PostgreSQL a failed statement poisons
        the whole transaction until it is rolled back, so the recovery lookup and the save that
        follow it would both raise. The student would see the duplicate they submitted turn
        into a server error, and the job they had actually entered first would be the only one
        in their tracker.
        """
        profile = make_profile(db)
        url = "https://careers.example.com/de-2026"
        service.add_manual_job(db, profile.id, title="Data Engineer", url=url)

        _blind_once(monkeypatch, "_find_listing", lambda args: args[1] == "manual")
        service.add_manual_job(db, profile.id, title="Data Engineer", url=url)

        # The session still works afterwards: a different job entered next still lands.
        later = service.add_manual_job(
            db, profile.id, title="Analytics Engineer", url="https://careers.example.com/ae"
        )

        assert db.get(SavedJob, later.id) is not None
        titles = {
            listing.title
            for listing in db.query(JobListing).filter(JobListing.provider == "manual").all()
        }
        assert titles == {"Data Engineer", "Analytics Engineer"}

    def test_saving_a_manual_listing_twice_is_still_one_row(self, db):
        """The per-profile unique constraint on saved_jobs, reached through the manual path.
        Entering the job and then tapping save on its detail screen must not open a second
        pipeline entry for the same job."""
        profile = make_profile(db)
        saved = service.add_manual_job(
            db, profile.id, title="Data Engineer", url="https://careers.example.com/de"
        )

        again = service.save_job(db, profile.id, saved.listing_id, "applied")

        assert again.id == saved.id
        assert db.query(SavedJob).filter(SavedJob.profile_id == profile.id).count() == 1

    def test_two_students_submitting_the_same_link_at_once_do_not_collide(self, db):
        """The deduplication key is scoped by profile, so this is not a race at all -- which
        is the point. Were it global, one of these two would silently adopt the other's row
        and read their typing."""
        mine = make_profile(db)
        theirs = make_profile(db)
        url = "https://careers.example.com/shared"

        first = service.add_manual_job(db, mine.id, title="As I typed it", url=url)
        second = service.add_manual_job(db, theirs.id, title="As they typed it", url=url)

        assert first.listing_id != second.listing_id
        assert db.get(JobListing, first.listing_id).created_by_profile_id == mine.id
        assert db.get(JobListing, second.listing_id).created_by_profile_id == theirs.id


class TestMatchInsertRace:
    def test_a_lost_match_race_does_not_discard_the_matches_already_scored(
        self, db, monkeypatch
    ):
        profile = make_profile(db)
        search_profile = service.get_or_create_search_profile(db, profile.id)
        search_profile.target_role_title = "Data Analyst"
        search_profile.normalized_skills = ["sql", "python"]
        db.commit()

        listings = []
        for job_id in ("a", "b", "c"):
            listing = JobListing(
                provider="fake",
                provider_job_id=job_id,
                url=f"https://jobs.example.com/{job_id}",
                title="Data Analyst",
                fetched_at=NOW,
            )
            db.add(listing)
            listings.append(listing)
        db.commit()

        # The concurrent request's match for the middle listing, already committed.
        service._upsert_match(db, search_profile, listings[1])
        db.commit()

        _blind_once(monkeypatch, "_find_match", lambda args: args[2] == listings[1].id)

        for listing in listings:
            service._upsert_match(db, search_profile, listing)
        db.commit()

        scored = service.get_saved_job_matches(
            db, profile.id, [listing.id for listing in listings]
        )
        # Before the fix the rollback took the first listing's match with it, so a page that
        # rendered three jobs carried a score for only some of them.
        assert set(scored) == {listing.id for listing in listings}


class TestAlertDeliveryRace:
    def test_an_already_announced_job_does_not_discard_this_run_s_other_deliveries(self, db):
        """The duplicate-suppression mechanism must not defeat itself.

        Drives the real alerts.run_alert against a fake provider, so this exercises the actual
        delivery loop rather than a copy of it. No simulation is needed either: the duplicate
        is a real row hitting the real unique constraint, which is what happens on every run
        after the first.

        The ordering is deliberate. run_alert records deliveries in the order search() returns
        listings, which for two otherwise-identical adverts is ascending listing id. Both
        listings are therefore created up front, and only the *higher* id is announced on the
        first run -- so the second run processes a new advert before it meets the duplicate.
        That is the order in which the bug bites: a bare rollback on the duplicate discarded
        the delivery just recorded for the new one, and the student was told about that job
        again on the next run, and the run after that.
        """
        profile = make_profile(db, subscribed=True)
        search_profile = service.get_or_create_search_profile(db, profile.id)
        search_profile.target_role_title = "Data Analyst"
        db.commit()

        first, second = (
            service._upsert_listing(db, provider_job("first"), FakeProvider(), NOW),
            service._upsert_listing(db, provider_job("second"), FakeProvider(), NOW),
        )
        db.commit()
        assert first.id < second.id

        alert = JobAlert(profile_id=profile.id, name="Dubai data roles", is_enabled=True)
        db.add(alert)
        db.commit()

        # Run one announces only the higher-id advert.
        status, count = alerts.run_alert(
            db, alert, profile, StubProvider([provider_job("second")]), now=NOW
        )
        assert (status, count) == ("success", 1)

        # Run two sees the new lower-id advert first, then the one already announced.
        status, count = alerts.run_alert(
            db,
            alert,
            profile,
            StubProvider([provider_job("first"), provider_job("second")]),
            now=NOW + datetime.timedelta(days=1),
        )

        assert (status, count) == ("success", 1)
        delivered = {row.listing_id for row in db.query(JobAlertDelivery).all()}
        # Before the fix this was {second.id}: the duplicate's rollback undid the delivery
        # recorded for `first` moments earlier, while the student had already been notified
        # about it -- so the next run would announce it a second time.
        assert delivered == {first.id, second.id}

    def test_a_re_run_over_the_same_jobs_enqueues_no_second_notification(self, db):
        """Duplicate suppression is only worth anything if it reaches the notification.

        The delivery rows are the mechanism; what the student actually experiences is the
        notification. This asserts the end of the chain: running the same alert twice over the
        same adverts produces exactly one message, not two.
        """
        from app.models.notification_event import NotificationEvent

        profile = make_profile(db, subscribed=True)
        search_profile = service.get_or_create_search_profile(db, profile.id)
        search_profile.target_role_title = "Data Analyst"
        db.commit()
        alert = JobAlert(profile_id=profile.id, name="Dubai data roles", is_enabled=True)
        db.add(alert)
        db.commit()

        jobs = [provider_job("a"), provider_job("b")]
        alerts.run_alert(db, alert, profile, StubProvider(jobs), now=NOW)
        alerts.run_alert(
            db, alert, profile, StubProvider(jobs), now=NOW + datetime.timedelta(days=1)
        )

        sent = db.query(NotificationEvent).filter(NotificationEvent.kind == "job_alert").all()
        assert len(sent) == 1, "the same adverts must not be announced twice"

    def test_a_second_run_with_nothing_new_announces_nothing(self, db):
        """The other half of the contract: suppression still suppresses."""
        profile = make_profile(db, subscribed=True)
        search_profile = service.get_or_create_search_profile(db, profile.id)
        search_profile.target_role_title = "Data Analyst"
        db.commit()

        alert = JobAlert(profile_id=profile.id, name="Dubai data roles", is_enabled=True)
        db.add(alert)
        db.commit()

        jobs = [provider_job("a"), provider_job("b")]
        assert alerts.run_alert(db, alert, profile, StubProvider(jobs), now=NOW) == ("success", 2)
        assert alerts.run_alert(
            db, alert, profile, StubProvider(jobs), now=NOW + datetime.timedelta(days=1)
        ) == ("no_new_jobs", 0)
        assert db.query(JobAlertDelivery).count() == 2
