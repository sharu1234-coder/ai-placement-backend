"""Bottleneck detection and impact x effort ranking.

The headline test in this file is `test_a_strong_technical_profile_with_weak_interviews`. It is
the scenario the whole feature exists for, and the one a naive "lowest score wins" engine gets
wrong: a student with strong code, strong proof and a strong resume who keeps interviewing
badly must be told to practise interviewing, not to go learn another framework.

Everything else here is about not overclaiming: a thin profile does not get a confident answer,
a constraint nobody can act on this month does not become the recommended action, and a skill
the student has already mastered stops being suggested.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.schemas.next_best_action import NextBestActionRead
from app.services.career_strategy import bottleneck, diminishing_returns, gaps, prioritization, signals
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_interview_evaluation,
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


def _decide(db, profile, nba=None):
    bundle = signals.collect(db, profile, now=NOW)
    found = gaps.classify(bundle)
    series = diminishing_returns.analyse(bundle)
    return bottleneck.detect(bundle, found, series, nba)


def _strong_technical_profile(db):
    """Strong code, strong proof, strong resume. The technical side is genuinely done."""
    role = make_role(db, "Data Engineer", {"SQL": 1.0, "Python": 1.0})
    profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                           graduation_year=2026, experience_level="fresher")
    for skill in ("SQL", "Python"):
        add_assessment_result(db, profile, {skill: 0.92}, created_at=days_ago(3))
        add_project_build(db, profile, title=f"{skill} Pipeline", stack_summary=skill)
    add_resume(db, profile, skills=["sql", "python"])
    return profile


class TestTheHeadlineScenario:
    def test_a_strong_technical_profile_with_weak_interviews(self, db):
        """Technical strong, resume strong, projects strong, interview communication weak.
        The bottleneck must be the interview, not another technology."""
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25, structure=0.3)
        add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=3)

        decision = _decide(db, profile)

        assert decision.primary is not None
        assert decision.primary.category == "INTERVIEW_GAP"
        assert decision.primary.subject == "interview:clarity"
        assert decision.primary.impact == "very_high"
        assert decision.primary.effort == "low"
        assert decision.primary.destination == "interview"

    def test_it_does_not_recommend_learning_another_technology(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25)
        add_weakness_record(db, profile, "interview:clarity", occurrences=3)

        decision = _decide(db, profile)
        assert "Learn" not in decision.primary.recommended_action
        # The proved skills must not appear anywhere in the queue as things to go study.
        queue_subjects = {item.subject for item in decision.queue}
        assert "SQL" not in queue_subjects
        assert "Python" not in queue_subjects

    def test_the_strengths_are_what_keep_the_skills_out_of_the_queue(self, db):
        """The mechanism, asserted directly: proved skills generate no gap, so they cannot be
        recommended. Strip the evidence and the same skills reappear as work to do -- which is
        what shows the interview verdict above came from the evidence, not from a hardcoded
        preference for interview gaps."""
        evidenced = _strong_technical_profile(db)
        add_interview_evaluation(db, evidenced, clarity=0.25)
        add_weakness_record(db, evidenced, "interview:clarity", occurrences=3)
        assert not [i for i in _decide(db, evidenced).queue if i.subject in ("SQL", "Python")]

        role = make_role(db, "Data Engineer 2", {"SQL": 1.0, "Python": 1.0})
        bare = make_profile(db, user_id=2, target_role_id=role.id, degree="B.Tech", branch="CSE",
                            graduation_year=2026, experience_level="fresher")
        add_interview_evaluation(db, bare, clarity=0.25)
        add_weakness_record(db, bare, "interview:clarity", occurrences=3)

        bare_queue = {i.subject for i in _decide(db, bare).queue}
        assert "SQL" in bare_queue and "Python" in bare_queue


class TestBottleneckSelection:
    def test_it_is_not_simply_the_lowest_score(self, db):
        """A mediocre-but-peripheral skill must not outrank a severe core problem just because
        its raw number is lower."""
        role = make_role(db, "SDE", {"SQL": 1.0, "Figma": 0.2})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.55, "Figma": 0.10}, created_at=days_ago(2))

        decision = _decide(db, profile)
        # Figma scored far lower, but it is barely weighted for this role.
        assert decision.primary.subject == "SQL"

    def test_a_mastered_skill_is_damped_out_of_contention(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 0.8})
        profile = make_profile(db, target_role_id=role.id)
        for index, score in enumerate([0.93, 0.94, 0.92]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(10 - index))

        decision = _decide(db, profile)
        # SQL is stable and strong across three results; Kafka has nothing at all.
        assert decision.primary.subject == "Kafka"

    def test_an_unactionable_constraint_never_becomes_the_recommended_action(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.92}, created_at=days_ago(3))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_resume(db, profile, skills=["sql"])
        add_interview_evaluation(db, profile)
        add_tracked_job(db, profile, components={"experience_fit": 0.1}, listing_id_seed=1, applied=False)

        decision = _decide(db, profile)
        # The experience gap is real and is reported, but you cannot practise your way to
        # seniority, so it must never be the recommended action. Being damped out of first
        # place is the correct outcome, not an omission.
        assert decision.primary.category != "EXPERIENCE_GAP"
        experience_items = [item for item in decision.queue if item.category == "EXPERIENCE_GAP"]
        for item in experience_items:
            assert item.status == "BLOCKED"

    def test_ties_break_deterministically(self, db):
        """Two identically-scoring gaps must not reshuffle between requests -- a queue that
        reorders itself would also make history disagree about what changed."""
        role = make_role(db, "SDE", {"SQL": 1.0, "Redis": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)

        first = _decide(db, profile)
        second = _decide(db, profile)
        assert first.primary.id == second.primary.id
        assert [i.id for i in first.queue] == [i.id for i in second.queue]
        # Same score, same effort: the trailing subject key is what settles it, alphabetically.
        assert first.primary.subject == "Redis"

    def test_a_proven_skill_generates_no_work_and_the_state_moves_on(self, db):
        """SQL is assessed, built and on the resume -- there is nothing left to recommend about
        it, so it must appear nowhere. What the student is pointed at instead is whatever real
        gap remains, and the state names that rather than inventing SQL work to fill the screen.
        """
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.92}, created_at=days_ago(3))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_resume(db, profile, skills=["sql"])
        add_interview_evaluation(db, profile)

        decision = _decide(db, profile)
        assert "SQL" not in {item.subject for item in decision.queue}
        assert decision.primary is None or decision.primary.subject != "SQL"
        # The remaining gap is the profile, and the state says exactly that -- the headline is
        # always the primary bottleneck's own category, never a separate judgement.
        assert decision.state == "FOCUS_ON_PROFILE"
        assert decision.primary.category == "PROFILE_GAP"

    def test_the_queue_is_capped_rather_than_becoming_a_task_dump(self, db):
        role = make_role(db, "SDE", {f"Skill{n}": 1.0 for n in range(12)})
        profile = make_profile(db, target_role_id=role.id)
        decision = _decide(db, profile)
        assert len(decision.queue) <= bottleneck.MAX_QUEUE_ITEMS


class TestStates:
    def test_no_target_role(self, db):
        profile = make_profile(db)
        decision = _decide(db, profile)
        assert decision.state == "FOCUS_ON_TARGET_ROLE"
        assert decision.primary is None
        assert "target role" in decision.state_reason.lower()

    def test_not_enough_data(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        decision = _decide(db, profile)
        # A role and nothing else: no confident answer is available, and none is given.
        assert decision.state == "INSUFFICIENT_INFORMATION"
        assert decision.primary is None
        assert decision.queue == []

    def test_the_state_names_the_bottlenecks_own_category(self, db):
        """A measured-weak core skill is a SKILL_GAP, so the state is FOCUS_ON_SKILLS. The state
        is not a second opinion about severity -- it is the primary bottleneck restated in the
        student's terms, which is what stops the headline disagreeing with the card below it."""
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.2}, created_at=days_ago(2))
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)

        decision = _decide(db, profile)
        assert decision.primary.category == "SKILL_GAP"
        assert decision.state == "FOCUS_ON_SKILLS"

    def test_the_state_always_matches_the_primary_bottleneck(self, db):
        """The invariant behind the two tests above, checked directly against the mapping table
        rather than against one hand-picked scenario."""
        role = make_role(db, "Data Engineer", {"SQL": 1.0, "Python": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.9, "Python": 0.9}, created_at=days_ago(3))
        add_project_build(db, profile, title="Pipeline", stack_summary="SQL Python")
        add_resume(db, profile, skills=["sql", "python"])
        add_interview_evaluation(db, profile, clarity=0.2)
        add_weakness_record(db, profile, "interview:clarity", occurrences=3)

        decision = _decide(db, profile)
        assert decision.primary is not None
        assert decision.state == bottleneck._STATE_BY_CATEGORY[decision.primary.category]
        assert decision.state_reason == bottleneck._STATE_REASONS[decision.state]

    def test_the_interview_scenario_reaches_the_interview_state(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25, structure=0.3)
        add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=3)
        assert _decide(db, profile).state == "FOCUS_ON_INTERVIEW"

    def test_every_state_is_declared(self, db):
        profile = make_profile(db)
        assert _decide(db, profile).state in bottleneck.STRATEGY_STATES


class TestNextBestActionComposition:
    def test_it_adopts_the_existing_services_recommendation(self, db):
        """Career Strategy must not run a second recommendation ladder. When NBA already has an
        opinion about this subject, that opinion is the action."""
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.2}, created_at=days_ago(2))
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)

        nba = NextBestActionRead(
            has_recommendation=True,
            tier="assessment_weakness",
            title="Practice SQL joins and indexing",
            action_type="practice",
            estimated_minutes=10,
            reasons=["Your last assessment flagged SQL as a weak area."],
            skill="SQL",
        )
        decision = _decide(db, profile, nba)
        assert decision.primary.subject == "SQL"
        assert decision.primary.recommended_action == "Practice SQL joins and indexing"
        assert "Your last assessment flagged SQL as a weak area." in decision.primary.evidence

    def test_it_falls_back_when_nba_is_talking_about_something_else(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.2}, created_at=days_ago(2))
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)

        nba = NextBestActionRead(
            has_recommendation=True, tier="stale_skill", title="Refresh your Kafka evidence",
            action_type="practice", estimated_minutes=10, reasons=[], skill="Kafka",
        )
        decision = _decide(db, profile, nba)
        # Silently using NBA's Kafka advice for an SQL bottleneck would be worse than useless.
        assert "Kafka" not in decision.primary.recommended_action
        assert decision.primary.recommended_action == "Practise SQL"

    def test_no_nba_at_all_still_produces_an_action(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.2}, created_at=days_ago(2))
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)
        assert _decide(db, profile, None).primary.recommended_action


class TestDontDoThisYet:
    def test_a_mastered_area_is_explicitly_deprioritised(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        for index, score in enumerate([0.93, 0.94, 0.92]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(10 - index))
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)

        decision = _decide(db, profile)
        deferred = {item.subject: item for item in decision.do_not_do_yet}
        # SQL is mastered, so it produces no *gap* -- the deferred list has to reach the
        # strength directly or it could never name it.
        assert "SQL" in deferred
        assert deferred["SQL"].category is None
        # The series' own wording, which carries the actual numbers.
        assert "already a strength" in deferred["SQL"].reason
        assert "93%" in deferred["SQL"].reason
        assert deferred["SQL"].title == "More practice on SQL"

    def test_it_says_what_to_do_instead_rather_than_only_what_to_avoid(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        for index, score in enumerate([0.93, 0.94, 0.92]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(10 - index))
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)

        for item in _decide(db, profile).do_not_do_yet:
            assert item.instead is not None
            # Never "this is useless" -- always "something else is worth more right now".
            for dismissive in ("useless", "pointless", "waste"):
                assert dismissive not in item.reason.lower()

    def test_the_primary_bottleneck_is_never_in_the_deferred_list(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25)
        add_weakness_record(db, profile, "interview:clarity", occurrences=3)

        decision = _decide(db, profile)
        assert decision.primary.title not in {item.title for item in decision.do_not_do_yet}

    def test_the_deferred_list_is_capped(self, db):
        role = make_role(db, "SDE", {f"Skill{n}": 0.2 for n in range(10)})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile, clarity=0.2)
        assert len(_decide(db, profile).do_not_do_yet) <= bottleneck.MAX_DEFERRED_ITEMS


class TestImpactAndEffort:
    def test_impact_and_effort_are_bands_not_raw_numbers(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25)
        decision = _decide(db, profile)
        assert decision.primary.impact in prioritization.IMPACT_BANDS
        assert decision.primary.effort in prioritization.EFFORT_BANDS
        for item in decision.queue:
            assert item.impact in prioritization.IMPACT_BANDS
            assert item.effort in prioritization.EFFORT_BANDS

    def test_a_profile_field_never_outranks_a_missing_core_skill(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)

        decision = _decide(db, profile)
        assert decision.primary.category == "SKILL_GAP"
        for item in decision.queue:
            if item.category == "PROFILE_GAP":
                assert item.impact in ("low", "medium")

    def test_learning_from_scratch_costs_more_effort_than_drilling(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.3}, created_at=days_ago(2))
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)

        by_subject = {item.subject: item for item in _decide(db, profile).queue}
        assert by_subject["SQL"].effort == "medium"  # ability: they have the material
        assert by_subject["Kafka"].effort == "high"  # knowledge: from scratch

    def test_every_factor_that_moved_the_score_is_explained(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        gap = gaps.classify(signals.collect(db, profile, now=NOW))[0]
        priority = prioritization.score(gap, series=None, evidence_confidence=None)
        assert priority.factors.notes
        assert priority.factors.category_weight == 1.0

    def test_unknown_confidence_is_a_full_deficit_not_a_middling_one(self):
        gap = gaps.StrategyGap(
            category="SKILL_GAP", subject="SQL", subject_kind="skill", severity="high",
            sub_kind="knowledge", role_weight=1.0, signal_keys=["evidence:SQL"], destination="roadmap",
            reason="none",
        )
        # We have nothing, and nothing is the largest shortfall -- not a middle value.
        assert prioritization.score(gap, evidence_confidence=None).factors.evidence_deficit == 1.0


class TestConfidenceAndProvenance:
    def test_a_thin_profile_does_not_produce_a_confident_answer(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)
        assert _decide(db, profile).primary.confidence in ("low", "moderate")

    def test_every_conclusion_is_traceable_to_signals(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25)
        decision = _decide(db, profile)
        assert decision.primary.created_from_signals
        assert decision.primary.decision_signals
        assert decision.primary.reason_code.startswith("INTERVIEW_GAP__")

    def test_affected_jobs_name_only_listings_the_match_engine_flagged(self, db):
        role = make_role(db, "SDE", {"Kafka": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_resume(db, profile, skills=["python"])
        add_interview_evaluation(db, profile)
        listing = add_tracked_job(db, profile, missing_skills=["Kafka"], listing_id_seed=1, applied=True)
        add_tracked_job(db, profile, missing_skills=["Redis"], listing_id_seed=2, applied=True)

        decision = _decide(db, profile)
        kafka = next(item for item in decision.queue if item.subject == "Kafka")
        assert kafka is not None
        assert decision.primary.affected_job_ids in ([listing.id], [])

    def test_no_output_field_resembles_a_hiring_probability(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25)
        decision = _decide(db, profile)
        serialised = decision.model_dump_json().lower()
        for banned in ("hiring_probability", "probability", "chance_of", "offer_probability"):
            assert banned not in serialised

    def test_a_decision_cannot_be_mutated(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25)
        decision = _decide(db, profile)
        with pytest.raises(ValidationError):
            decision.primary.impact = "low"


class TestDeterminism:
    def test_the_same_state_produces_the_same_decision(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25)
        add_tracked_job(db, profile, missing_skills=["Kafka"], listing_id_seed=1, applied=True)
        assert _decide(db, profile).model_dump() == _decide(db, profile).model_dump()

    def test_the_strategy_version_is_stamped_on_every_decision(self, db):
        profile = _strong_technical_profile(db)
        add_interview_evaluation(db, profile, clarity=0.25)
        decision = _decide(db, profile)
        assert decision.strategy_version == bottleneck.STRATEGY_VERSION
        assert decision.primary.strategy_version == bottleneck.STRATEGY_VERSION
