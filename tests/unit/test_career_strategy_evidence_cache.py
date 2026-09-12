"""The strategy read hands its evidence to Career Twin instead of making it fetch again.

Career Strategy builds a signal bundle that already contains an evidence view per skill, then
asked career_twin_service for readiness -- and that service ran the identical evidence pass a
second time. The obvious way to remove the duplicate is to compute readiness in the strategy
layer from the bundle it already holds. That would be a second readiness engine, free to
disagree with Career Twin about the same student on the same screen, and it is exactly what this
whole layer exists to avoid.

So the views are handed over as a *cache* instead. career_twin_service still computes readiness,
still owns that number, and still fetches anything the caller did not bring. The optimisation can
therefore only change the cost of a read, never its result -- which is precisely what these tests
pin down: one asserts the number is unchanged, the other asserts the reads went down.

The fallback test is the important one. A cache that silently dropped a skill it had not been
given would produce a *quietly different* readiness, which is the worst possible failure here:
no error, no crash, just a number that no longer matches the Career Twin screen.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.services import career_twin_service
from app.services.career_strategy import service as strategy_service
from app.services.career_strategy import signals as signals_module
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_job_preferences,
    add_project_build,
    add_resume,
    days_ago,
    make_profile,
    make_role,
)


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _counting_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    counter = {"n": 0, "on": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if counter["on"] and statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    return sessionmaker(bind=engine)(), counter


def _student(db, user_id=1, role_name="Backend Engineer"):
    role = make_role(db, role_name, {"Python": 1.0, "SQL": 0.9, "Docker": 0.6})
    profile = make_profile(db, user_id=user_id, target_role_id=role.id, degree="B.Tech",
                           branch="CSE", graduation_year=2026, experience_level="fresher")
    add_resume(db, profile, skills=["python", "sql"])
    add_job_preferences(db, profile, normalized_skills=["python", "sql"])
    add_assessment_result(db, profile, {"Python": 0.93, "SQL": 0.71}, created_at=days_ago(3))
    add_project_build(db, profile, title="Python Service", stack_summary="Python")
    add_assessment_result(db, profile, {"Python": 0.9}, created_at=days_ago(2))
    return profile


class TestTheCacheChangesCostNotAnswers:
    def test_readiness_is_identical_with_and_without_the_cache(self, db):
        """The whole safety argument in one assertion."""
        profile = _student(db)
        bundle = signals_module.collect(db, profile, now=NOW)

        uncached = career_twin_service.get_current(db, profile)
        cached = career_twin_service.get_current(db, profile, evidence=bundle.evidence_by_skill)

        assert cached.readiness_percent == uncached.readiness_percent
        assert cached.is_estimate == uncached.is_estimate
        assert [item.skill for item in cached.skills] == [item.skill for item in uncached.skills]
        assert [item.state for item in cached.skills] == [item.state for item in uncached.skills]
        assert [item.confidence_band for item in cached.skills] == [
            item.confidence_band for item in uncached.skills
        ]

    def test_the_strategy_readiness_still_equals_career_twins_own_number(self, db):
        """Composition is only worth anything if the two screens still agree. This is the same
        invariant the integration suite asserts, re-checked on the optimised path."""
        profile = _student(db, user_id=2, role_name="Backend Engineer 2")
        view = strategy_service.compute(db, profile, now=NOW)
        assert view.twin.readiness_percent == career_twin_service.get_current(db, profile).readiness_percent

    def test_a_skill_the_caller_did_not_bring_is_still_fetched(self, db):
        """The fallback, and the failure mode worth fearing: a cache that dropped an unknown
        skill would return a quietly different readiness rather than an error."""
        profile = _student(db, user_id=3, role_name="Backend Engineer 3")
        full = career_twin_service.get_current(db, profile)

        bundle = signals_module.collect(db, profile, now=NOW)
        # Deliberately withhold one role skill from the cache.
        partial = {k: v for k, v in bundle.evidence_by_skill.items() if k != "SQL"}
        assert "SQL" not in partial

        with_partial = career_twin_service.get_current(db, profile, evidence=partial)

        assert with_partial.readiness_percent == full.readiness_percent
        assert {item.skill for item in with_partial.skills} == {item.skill for item in full.skills}

    def test_an_empty_cache_behaves_exactly_like_no_cache(self, db):
        profile = _student(db, user_id=4, role_name="Backend Engineer 4")
        assert (
            career_twin_service.get_current(db, profile, evidence={}).readiness_percent
            == career_twin_service.get_current(db, profile).readiness_percent
        )

    def test_the_cache_removes_the_duplicate_evidence_read(self):
        """The point of the change, measured on exactly the call that changed.

        Comparing whole `compute()` runs would not isolate this: that path also asks
        next_best_action_service for a recommendation, and folding its queries into the
        comparison would measure something other than the thing under test.

        Stated as an inequality rather than against a fixed number. A magic constant here would
        need editing on every legitimate change, which is how such assertions end up being
        raised to match reality instead of investigated.
        """
        counting_db, counter = _counting_session()
        profile = _student(counting_db, user_id=5, role_name="Backend Engineer 5")
        bundle = signals_module.collect(counting_db, profile, now=NOW)
        assert bundle.evidence_by_skill, "sanity: the bundle really does carry evidence"

        counter["on"] = True
        career_twin_service.get_current(counting_db, profile)
        uncached = counter["n"]

        counter["n"] = 0
        career_twin_service.get_current(counting_db, profile, evidence=bundle.evidence_by_skill)
        cached = counter["n"]
        counter["on"] = False

        assert cached < uncached, f"expected the cache to remove reads: {cached} vs {uncached}"
