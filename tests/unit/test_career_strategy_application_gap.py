"""D4: "ready but not applying" is a different problem from "not ready and not applying".

Before this, both produced an identical medium-severity APPLICATION_GAP. That flattening cost
the product its single most valuable piece of advice: the student who has done everything and
simply has not pressed send ranked level with the student who still has three open skill gaps,
so the queue told them to keep preparing.

The severity is decided from gaps the same classification pass already found -- never from a
readiness figure computed here. A second readiness calculation inside the gap classifier would
be a second source of truth for the number the whole product turns on, free to disagree with
Career Twin on the same screen.

The five cases below are the full truth table the spec asks for. Two of them are the ones that
would break first if someone later "simplified" this: an unanswered application-readiness input
must block the confident wording (we cannot claim someone is prepared when we cannot see their
resume), and EXPERIENCE_GAP must not block it (a fresher cannot acquire three years this month,
and letting that veto the advice would silence it for exactly the students who need it).

Nothing here estimates a chance of being hired. "Nothing we can measure is still in your way"
is a statement about our own measurements, not a prediction about employers.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.services.career_strategy import gaps as gaps_module
from app.services.career_strategy import signals as signals_module
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_interview_evaluation,
    add_job_preferences,
    add_project_build,
    add_resume,
    add_tracked_job,
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


def _application_gap(db, profile):
    bundle = signals_module.collect(db, profile, now=NOW)
    found = gaps_module.classify(bundle)
    return next((gap for gap in found if gap.category == "APPLICATION_GAP"), None)


def _prepared_student(db, user_id=1, role_name="Backend Engineer"):
    """Everything the product can measure is in good shape: proven skill, resume on file, an
    interview actually recorded. The only thing missing is an application."""
    role = make_role(db, role_name, {"Python": 1.0})
    profile = make_profile(db, user_id=user_id, target_role_id=role.id, degree="B.Tech",
                           branch="CSE", graduation_year=2026, experience_level="fresher")
    add_resume(db, profile, skills=["python"])
    add_job_preferences(db, profile, normalized_skills=["python"])
    add_assessment_result(db, profile, {"Python": 0.93}, created_at=days_ago(3))
    add_project_build(db, profile, title="Python Service", stack_summary="Python")
    add_assessment_result(db, profile, {"Python": 0.9}, created_at=days_ago(2))
    add_interview_evaluation(db, profile, clarity=0.85, structure=0.8)
    return profile


class TestReadyButNotApplying:
    def test_a_prepared_student_who_is_not_applying_is_the_top_priority(self, db):
        """Case 1. The headline of D4."""
        profile = _prepared_student(db)
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.8,
                        components={"experience_fit": 0.9}, listing_id_seed=701)

        gap = _application_gap(db, profile)
        assert gap is not None
        assert gap.severity == "high"
        assert "turning that preparation into applications" in gap.reason

    def test_an_unprepared_student_who_is_not_applying_stays_mid_priority(self, db):
        """Case 2. Same tracker observation, opposite meaning: telling someone with an open
        interview problem that applying is their top priority would be bad advice."""
        role = make_role(db, "Data Engineer", {"Python": 1.0, "SQL": 0.9})
        profile = make_profile(db, user_id=2, target_role_id=role.id, degree="B.Tech",
                               branch="CSE", graduation_year=2026, experience_level="fresher")
        add_resume(db, profile, skills=["python"])
        add_job_preferences(db, profile, normalized_skills=["python"])
        add_interview_evaluation(db, profile, clarity=0.15, structure=0.2)
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.5,
                        components={"experience_fit": 0.5}, listing_id_seed=702)

        gap = _application_gap(db, profile)
        assert gap is not None
        assert gap.severity == "medium"
        assert "turning that preparation into applications" not in gap.reason

    def test_a_prepared_student_who_is_applying_produces_no_application_gap(self, db):
        """Case 3. Nothing to report -- and nothing invented to fill the space."""
        profile = _prepared_student(db, user_id=3, role_name="Backend Engineer 3")
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.8,
                        components={"experience_fit": 0.9}, applied=True, listing_id_seed=703)

        gap = _application_gap(db, profile)
        assert gap is None or gap.subject != "applications"

    def test_unknown_readiness_does_not_earn_the_confident_wording(self, db):
        """Case 4, and the one most likely to regress. No resume on file means
        application-readiness cannot be judged at all. "You appear prepared" would then be a
        claim resting on an absence of evidence rather than on evidence."""
        role = make_role(db, "Backend Engineer 4", {"Python": 1.0})
        profile = make_profile(db, user_id=4, target_role_id=role.id, degree="B.Tech",
                               branch="CSE", graduation_year=2026, experience_level="fresher")
        add_job_preferences(db, profile, normalized_skills=["python"])
        add_assessment_result(db, profile, {"Python": 0.93}, created_at=days_ago(3))
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.8,
                        components={"experience_fit": 0.9}, listing_id_seed=704)

        gap = _application_gap(db, profile)
        if gap is not None:
            assert gap.severity == "medium"
            assert "turning that preparation into applications" not in gap.reason

    def test_a_strong_blocker_keeps_the_application_gap_secondary(self, db):
        """Case 5. A high-severity skill gap must win."""
        role = make_role(db, "Data Engineer 5", {"Python": 1.0, "Kafka": 0.95})
        profile = make_profile(db, user_id=5, target_role_id=role.id, degree="B.Tech",
                               branch="CSE", graduation_year=2026, experience_level="fresher")
        add_resume(db, profile, skills=["python"])
        add_job_preferences(db, profile, normalized_skills=["python"])
        add_interview_evaluation(db, profile, clarity=0.8)
        add_assessment_result(db, profile, {"Kafka": 0.2}, created_at=days_ago(2))
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.6,
                        components={"experience_fit": 0.7}, listing_id_seed=705)

        gap = _application_gap(db, profile)
        if gap is not None:
            assert gap.severity == "medium"

    def test_an_experience_gap_alone_does_not_suppress_the_advice(self, db):
        """A fresher will always carry an experience gap against real adverts. If that vetoed
        "you are ready, start applying", the advice would never fire for the group it exists
        for. EXPERIENCE_GAP is reported but never actionable -- and never a blocker here."""
        profile = _prepared_student(db, user_id=6, role_name="Backend Engineer 6")
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.8,
                        components={"experience_fit": 0.1}, listing_id_seed=706)

        gap = _application_gap(db, profile)
        assert gap is not None
        assert gap.severity == "high"


    def test_tracking_only_poorly_matched_jobs_is_not_a_conversion_problem(self, db):
        """Suitability matters. A queue of adverts the match engine rates badly is not a student
        who has stopped short of applying -- pushing them to apply anyway would be worse advice
        than silence."""
        profile = _prepared_student(db, user_id=8, role_name="Backend Engineer 8")
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.25,
                        components={"experience_fit": 0.3}, listing_id_seed=708)

        gap = _application_gap(db, profile)
        if gap is not None:
            assert gap.severity == "medium"
            assert "turning that preparation into applications" not in gap.reason

    def test_a_rejected_application_still_counts_as_an_application(self, db):
        """applied_at is the authority, never the status string. The student did the thing; a
        rejection does not un-do it, and reading status to decide otherwise would tell someone
        who has applied to twelve jobs that they have never applied to any."""
        profile = _prepared_student(db, user_id=9, role_name="Backend Engineer 9")
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.8,
                        components={"experience_fit": 0.9}, applied=True, status="rejected",
                        listing_id_seed=709)

        gap = _application_gap(db, profile)
        assert gap is None or gap.subject != "applications"

    def test_a_closed_status_is_not_given_a_meaning_the_schema_does_not_define(self, db):
        """"closed" is undocumented. It is not read as applied, not read as not-applied, and not
        read at all -- applied_at answers the only question being asked."""
        profile = _prepared_student(db, user_id=10, role_name="Backend Engineer 10")
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.8,
                        components={"experience_fit": 0.9}, applied=False, status="closed",
                        listing_id_seed=710)

        # applied_at is null, so this is still "tracked but never applied" -- exactly as it would
        # be for any other status. The status string changed nothing.
        gap = _application_gap(db, profile)
        assert gap is not None
        assert gap.severity == "high"

    def test_no_probability_language_appears_in_the_reason(self, db):
        profile = _prepared_student(db, user_id=7, role_name="Backend Engineer 7")
        add_tracked_job(db, profile, matched_skills=["Python"], score=0.8,
                        components={"experience_fit": 0.9}, listing_id_seed=707)

        gap = _application_gap(db, profile)
        assert gap is not None
        lowered = gap.reason.lower()
        for banned in ("probability", "chance of", "likelihood", "guaranteed", "will get hired"):
            assert banned not in lowered
