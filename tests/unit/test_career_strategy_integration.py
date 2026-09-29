"""End-to-end consistency: every system telling the student the same story about one skill.

The product claim this feature rests on is that the app understands a whole career rather than
running a dozen disconnected tools. That claim is only true if the systems actually agree, and
agreement is exactly what unit tests scoped to one module cannot check -- each can be perfectly
correct while the screens contradict each other.

So these tests follow one skill along the real chain:

    resume -> evidence engine -> skill passport -> career twin -> job match -> career strategy

and assert at each hop that the verdict is the *same verdict*, not a plausible-looking second
opinion. The strongest of them is `test_one_skill_tells_one_story_across_every_system`, which
compares the actual values each service publishes rather than re-deriving them here -- a test
that recomputed the answer could agree with itself while the app disagreed with itself.

The second half is about the transition. A resume claim and a proven capability must never
collapse into one thing, so the same skill is walked from a bare resume mention (which tops out
at `supported`, because nothing independent backs it) to genuinely proven, and the strategy is
required to change its advice accordingly -- and, critically, to stop recommending work on it
once it is proven.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.services import (
    career_twin_service,
    evidence_engine,
    next_best_action_service,
    skill_passport_service,
)
from app.services.career_strategy import service as strategy_service
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_interview_evaluation,
    add_job_preferences,
    add_project_build,
    add_resume,
    add_tracked_job,
    add_weakness_record,
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


def _claimed_only(db, user_id=1, role_name="Backend Engineer"):
    """Python is on the resume and nowhere else -- the whole point of the evidence ladder.

    `role_name` and `user_id` are parameters because roles.name is unique: a test that needs
    both a claimed and a proven student in one database needs two of each.
    """
    role = make_role(db, role_name, {"Python": 1.0, "SQL": 0.9})
    profile = make_profile(db, user_id=user_id, target_role_id=role.id, degree="B.Tech",
                           branch="CSE", graduation_year=2026, experience_level="fresher")
    add_resume(db, profile, skills=["python"])
    add_job_preferences(db, profile, normalized_skills=["python"])
    return profile


def _proven(db, user_id=1, role_name="Backend Engineer"):
    """The same student, after actually proving Python: two scored assessments and a build."""
    profile = _claimed_only(db, user_id=user_id, role_name=role_name)
    add_assessment_result(db, profile, {"Python": 0.93}, created_at=days_ago(3))
    add_project_build(db, profile, title="Python Service", stack_summary="Python")
    add_assessment_result(db, profile, {"Python": 0.9}, created_at=days_ago(2))
    return profile


class TestOneStory:
    def test_a_resume_claim_is_never_treated_as_a_proven_skill(self, db):
        """The single most important invariant in the product.

        Note the exact ceiling being asserted. A resume mention is one self-asserted source, so
        evidence_engine reports it as `supported` -- not `claimed`, which means no evidence at
        all. What it can never reach is `strong` or `verified`, because both require a source
        the student did not assert about themselves. That ceiling is the whole distinction
        between "I say I know Python" and "I have shown I know Python", and every system in the
        chain has to respect it independently.
        """
        profile = _claimed_only(db)

        evidence = evidence_engine.get_skill_evidence(db, profile.id, "Python")
        assert evidence.state == "supported"
        assert evidence.state not in ("strong", "verified")
        assert all(item.self_asserted for item in evidence.items)

        passport = skill_passport_service.get_skill(db, profile, "Python")
        assert passport.state == evidence.state

        twin = career_twin_service.get_current(db, profile)
        python = next(item for item in twin.skills if item.skill == "Python")
        assert python.state == evidence.state

        # And the strategy agrees: this is still work to do, not something already banked.
        decision = strategy_service.compute(db, profile, now=NOW).decision
        assert "Python" in {item.subject for item in decision.queue}

    def test_one_skill_tells_one_story_across_every_system(self, db):
        """Each service is asked for its own published value and the values are compared. A test
        that recalculated the answer here could pass while the app contradicted itself."""
        profile = _proven(db)

        evidence = evidence_engine.get_skill_evidence(db, profile.id, "Python")
        passport = skill_passport_service.get_skill(db, profile, "Python")
        twin_item = next(
            item for item in career_twin_service.get_current(db, profile).skills if item.skill == "Python"
        )

        assert evidence.state == passport.state == twin_item.state
        assert evidence.confidence_band == passport.confidence_band == twin_item.confidence_band

    def test_proving_a_skill_removes_it_from_the_strategy(self, db):
        """The transition, end to end: the same skill goes from recommended work to not
        mentioned at all, purely because the evidence underneath it changed.

        Two students rather than one mutated in place, so the two decisions are genuinely
        independent rather than one being a stale read of the other.
        """
        claimed = _claimed_only(db, user_id=1, role_name="Backend Engineer")
        proven = _proven(db, user_id=2, role_name="Backend Engineer 2")

        before = strategy_service.compute(db, claimed, now=NOW).decision
        after = strategy_service.compute(db, proven, now=NOW).decision

        assert "Python" in {item.subject for item in before.queue}
        assert "Python" not in {item.subject for item in after.queue}
        assert after.primary is None or after.primary.subject != "Python"

    def test_the_strategy_readiness_is_career_twins_number(self, db):
        """Two readiness figures on two screens would make both untrustworthy."""
        profile = _proven(db)
        view = strategy_service.compute(db, profile, now=NOW)
        assert view.twin.evidence_coverage_percent == career_twin_service.get_current(db, profile).evidence_coverage_percent

    def test_the_strategy_and_next_best_action_do_not_give_conflicting_orders(self, db):
        """Career Strategy consumes the existing recommendation engine rather than running a
        second ladder beside it. Where both name the same subject, the wording must be NBA's."""
        profile = _proven(db)
        add_interview_evaluation(db, profile, clarity=0.2)
        add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=3)

        decision = strategy_service.compute(db, profile, now=NOW).decision
        nba = next_best_action_service.get_next_best_action(db, profile)

        if nba.has_recommendation and nba.skill is not None and decision.primary is not None:
            if nba.skill == decision.primary.subject:
                assert decision.primary.recommended_action == nba.title

    def test_a_job_match_gap_reaches_the_job_strategy_unchanged(self, db):
        """The match engine's own verdict about one advert, carried through rather than
        recomputed. A second match engine would be free to disagree with the Job Hunt screen."""
        profile = _proven(db)
        listing = add_tracked_job(db, profile, matched_skills=["Python"], missing_skills=["Kafka"],
                                  score=0.72, components={"experience_fit": 0.8}, listing_id_seed=501)

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert "Kafka" in result.missing_skills
        assert result.verified_skill_count >= 1  # Python is proven and counted as proven
        assert result.match_score == 0.72

    def test_a_proven_skill_is_never_a_blocker_on_a_job(self, db):
        profile = _proven(db)
        listing = add_tracked_job(db, profile, matched_skills=["Python"], missing_skills=["Kafka"],
                                  score=0.72, components={"experience_fit": 0.8}, listing_id_seed=502)

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert "Python" not in {blocker.subject for blocker in result.blockers}
        assert "Kafka" in {blocker.subject for blocker in result.blockers}

    def test_every_recommendation_lands_somewhere_real(self, db):
        """No dead ends. Every destination the strategy emits has to be a feature that exists;
        the Android route table maps exactly this set."""
        known = {"roadmap", "practice", "project_builder", "skill_passport", "resume_ats",
                 "interview", "job_hunt", "tracker", "career_path", "profile"}
        profile = _claimed_only(db)
        add_interview_evaluation(db, profile, clarity=0.2)
        add_weakness_record(db, profile, "interview:clarity", occurrences=3)
        add_tracked_job(db, profile, missing_skills=["Kafka"], listing_id_seed=503)

        decision = strategy_service.compute(db, profile, now=NOW).decision
        if decision.primary is not None:
            assert decision.primary.destination in known
        for item in decision.queue:
            assert item.destination in known

    def test_an_interview_weakness_outranks_proven_technical_skills(self, db):
        """The headline product scenario, checked through the full service rather than the
        engine in isolation: strong code, strong proof, weak interview."""
        profile = _proven(db)
        add_assessment_result(db, profile, {"SQL": 0.94}, created_at=days_ago(4))
        add_project_build(db, profile, title="SQL Warehouse", stack_summary="SQL")
        add_resume(db, profile, skills=["python", "sql"])
        add_interview_evaluation(db, profile, clarity=0.2, structure=0.25)
        add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=4)

        decision = strategy_service.compute(db, profile, now=NOW).decision
        assert decision.primary is not None
        assert decision.primary.category == "INTERVIEW_GAP"
        assert decision.state == "FOCUS_ON_INTERVIEW"
        assert not {"Python", "SQL"} & {item.subject for item in decision.queue}

    def test_nothing_anywhere_in_the_chain_emits_a_hiring_probability(self, db):
        """Checked across every serialised surface at once, so a new field on any one of them
        cannot slip through."""
        profile = _proven(db)
        listing = add_tracked_job(db, profile, matched_skills=["Python"], missing_skills=["Kafka"],
                                  listing_id_seed=504)

        payloads = [
            strategy_service.get_strategy(db, profile, is_premium=True, now=NOW).model_dump_json(),
            strategy_service.get_strategy(db, profile, is_premium=False, now=NOW).model_dump_json(),
            strategy_service.get_job_strategy(db, profile, listing.id, now=NOW).model_dump_json(),
            strategy_service.compare_targets(db, profile, is_premium=True, now=NOW).model_dump_json(),
            strategy_service.get_history(db, profile.id).model_dump_json(),
            career_twin_service.get_current(db, profile).model_dump_json(),
        ]
        for payload in payloads:
            lowered = payload.lower()
            for banned in ("hiring_probability", "probability_of", "chance_of_hiring",
                           "likelihood_of_offer", "placement_probability", "guaranteed"):
                assert banned not in lowered
