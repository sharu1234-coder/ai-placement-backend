"""Contradiction detection -- all seven kinds, each tested for firing and for not firing.

The negative tests carry as much weight as the positive ones. A detector that fires on an
absent source turns "we have not seen your interviews" into "your communication is weak", which
is a fabricated claim about a student, and the kind of bug that is invisible until someone
reads their own strategy screen and does not recognise themselves in it.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.services.career_strategy import contradictions, diminishing_returns, gaps, signals
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_ats_check,
    add_interview_evaluation,
    add_learning_topics,
    add_project_build,
    add_recruiter_attack,
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


def _detect(db, profile):
    return contradictions.detect(signals.collect(db, profile, now=NOW))


def _of(found, kind):
    return [c for c in found if c.kind == kind]


class TestResumeClaimExceedsEvidence:
    def test_it_fires_when_the_resume_names_an_unevidenced_skill(self, db):
        role = make_role(db, "SDE", {"AWS": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["aws"])

        found = _of(_detect(db, profile), "RESUME_CLAIM_EXCEEDS_EVIDENCE")
        assert found[0].subject == "AWS"
        assert found[0].reclassify_to == "EVIDENCE_GAP"

    def test_both_facts_survive_in_the_output(self, db):
        role = make_role(db, "SDE", {"AWS": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["aws"])

        found = _of(_detect(db, profile), "RESUME_CLAIM_EXCEEDS_EVIDENCE")[0]
        assert "resume presents AWS" in found.left_statement
        assert "Nothing independent" in found.right_statement
        assert found.left_signal_keys and found.right_signal_keys

    def test_the_wording_observes_rather_than_accuses(self, db):
        role = make_role(db, "SDE", {"AWS": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["aws"])

        explanation = _of(_detect(db, profile), "RESUME_CLAIM_EXCEEDS_EVIDENCE")[0].explanation
        assert "evidence we can see is limited" in explanation
        for accusation in ("lying", "false", "overstat", "exaggerat", "dishonest"):
            assert accusation not in explanation.lower()

    def test_it_does_not_fire_once_independent_evidence_exists(self, db):
        role = make_role(db, "SDE", {"AWS": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["aws"])
        add_assessment_result(db, profile, {"AWS": 0.85}, created_at=days_ago(2))
        assert not _of(_detect(db, profile), "RESUME_CLAIM_EXCEEDS_EVIDENCE")

    def test_it_does_not_fire_without_a_resume(self, db):
        role = make_role(db, "SDE", {"AWS": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        # No claim exists to exceed anything.
        assert not _of(_detect(db, profile), "RESUME_CLAIM_EXCEEDS_EVIDENCE")


class TestTechnicalStrongCommunicationWeak:
    def _strong_but_unclear(self, db, **dims):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.92}, created_at=days_ago(2))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_interview_evaluation(db, profile, **dims)
        return profile

    def test_it_fires_on_strong_evidence_with_weak_delivery(self, db):
        profile = self._strong_but_unclear(db, clarity=0.3)
        found = _of(_detect(db, profile), "TECHNICAL_STRONG_COMMUNICATION_WEAK")
        assert found[0].reclassify_to == "INTERVIEW_GAP"
        assert "interview:clarity" in found[0].right_signal_keys

    def test_it_ignores_technical_correctness(self, db):
        # A weak technical_correctness is a knowledge problem and is already an ABILITY_GAP;
        # routing it to communication practice would be the wrong screen entirely.
        profile = self._strong_but_unclear(db, technical_correctness=0.2)
        assert not _of(_detect(db, profile), "TECHNICAL_STRONG_COMMUNICATION_WEAK")

    def test_recruiter_attack_communication_also_triggers_it(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.92}, created_at=days_ago(2))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_recruiter_attack(db, profile, communication=0.3)
        assert _of(_detect(db, profile), "TECHNICAL_STRONG_COMMUNICATION_WEAK")

    def test_it_does_not_fire_without_strong_technical_evidence(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_interview_evaluation(db, profile, clarity=0.2)
        # Weak delivery on its own is just an INTERVIEW_GAP, not a contradiction.
        assert not _of(_detect(db, profile), "TECHNICAL_STRONG_COMMUNICATION_WEAK")

    def test_it_does_not_fire_when_delivery_is_fine(self, db):
        profile = self._strong_but_unclear(db)
        assert not _of(_detect(db, profile), "TECHNICAL_STRONG_COMMUNICATION_WEAK")


class TestLearningAheadOfPerformance:
    def test_it_fires_when_completed_learning_is_not_showing_up(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed"), ("SQL", "completed")])
        add_assessment_result(db, profile, {"SQL": 0.35}, created_at=days_ago(1))

        found = _of(_detect(db, profile), "LEARNING_AHEAD_OF_PERFORMANCE")[0]
        assert found.subject == "SQL"
        assert found.reclassify_to == "SKILL_GAP"
        assert "100%" in found.left_statement
        assert "35%" in found.right_statement

    def test_it_does_not_fire_when_the_learning_is_unfinished(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed"), ("SQL", "not_started"), ("SQL", "not_started")])
        add_assessment_result(db, profile, {"SQL": 0.35}, created_at=days_ago(1))
        # Scoring poorly on material you have not finished is not a contradiction.
        assert not _of(_detect(db, profile), "LEARNING_AHEAD_OF_PERFORMANCE")

    def test_it_does_not_fire_without_a_measurement(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed")])
        # Learning complete and never tested is not evidence of anything going wrong.
        assert not _of(_detect(db, profile), "LEARNING_AHEAD_OF_PERFORMANCE")

    def test_it_does_not_fire_when_performance_is_good(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed")])
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(1))
        assert not _of(_detect(db, profile), "LEARNING_AHEAD_OF_PERFORMANCE")


class TestResumeRichAtsWeak:
    def test_it_fires_on_a_full_resume_with_a_low_ats_score(self, db):
        profile = make_profile(db)
        resume = add_resume(db, profile, skills=["sql", "python", "aws", "airflow", "spark", "kafka"])
        add_ats_check(db, profile, resume, overall=45)

        found = _of(_detect(db, profile), "RESUME_RICH_ATS_WEAK")[0]
        assert found.reclassify_to == "RESUME_GAP"
        assert "45%" in found.right_statement
        assert "formatting and positioning" in found.explanation

    def test_it_does_not_fire_without_an_ats_check(self, db):
        profile = make_profile(db)
        add_resume(db, profile, skills=["sql", "python", "aws", "airflow", "spark", "kafka"])
        # Guessing at ATS alignment from a skill count would invent the number ats_scoring
        # exists to measure.
        assert not _of(_detect(db, profile), "RESUME_RICH_ATS_WEAK")

    def test_it_does_not_fire_on_a_thin_resume(self, db):
        profile = make_profile(db)
        resume = add_resume(db, profile, skills=["sql"])
        add_ats_check(db, profile, resume, overall=40)
        # A low score on a nearly-empty resume is not a presentation problem.
        assert not _of(_detect(db, profile), "RESUME_RICH_ATS_WEAK")

    def test_it_does_not_fire_when_the_ats_score_is_healthy(self, db):
        profile = make_profile(db)
        resume = add_resume(db, profile, skills=["sql", "python", "aws", "airflow", "spark", "kafka"])
        add_ats_check(db, profile, resume, overall=85)
        assert not _of(_detect(db, profile), "RESUME_RICH_ATS_WEAK")


class TestCapabilityOutsideTargetRole:
    def _misaligned(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 0.8, "Spark": 0.8})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["figma"])
        add_project_build(db, profile, title="Figma Design System", stack_summary="Figma")
        return profile

    def test_it_fires_when_strengths_sit_outside_the_target(self, db):
        profile = self._misaligned(db)
        found = _of(_detect(db, profile), "CAPABILITY_OUTSIDE_TARGET_ROLE")[0]
        assert "figma" in found.left_statement
        assert found.reclassify_to == "JOB_TARGETING_GAP"

    def test_it_leaves_the_conclusion_to_the_student(self, db):
        # Career Decision Lab already owns multi-target comparison; this only states both facts.
        profile = self._misaligned(db)
        explanation = _of(_detect(db, profile), "CAPABILITY_OUTSIDE_TARGET_ROLE")[0].explanation
        assert "worth a deliberate decision" in explanation

    def test_it_does_not_fire_when_the_role_is_also_well_evidenced(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 0.8, "Spark": 0.8})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["figma"])
        add_project_build(db, profile, title="Figma Design System", stack_summary="Figma")
        for skill in ("SQL", "Kafka", "Spark"):
            add_assessment_result(db, profile, {skill: 0.9}, created_at=days_ago(2))
        assert not _of(_detect(db, profile), "CAPABILITY_OUTSIDE_TARGET_ROLE")

    def test_it_does_not_fire_without_a_role(self, db):
        profile = make_profile(db)
        add_resume(db, profile, skills=["figma"])
        add_project_build(db, profile, title="Figma Design System", stack_summary="Figma")
        assert not _of(_detect(db, profile), "CAPABILITY_OUTSIDE_TARGET_ROLE")

    def test_it_does_not_fire_for_a_role_with_too_few_skills_to_compare(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["figma"])
        add_project_build(db, profile, title="Figma Design System", stack_summary="Figma")
        # One weighted skill is not enough for "your strengths are elsewhere" to be fair.
        assert not _of(_detect(db, profile), "CAPABILITY_OUTSIDE_TARGET_ROLE")


class TestPracticeWithoutImprovement:
    def test_it_fires_on_a_flat_low_series(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        for index, score in enumerate([0.45, 0.47, 0.44, 0.46]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(10 - index))

        found = _of(_detect(db, profile), "PRACTICE_WITHOUT_IMPROVEMENT")[0]
        assert found.subject == "SQL"
        assert "4 recorded SQL results" in found.left_statement
        assert "different format" in found.explanation

    def test_it_does_not_fire_below_the_minimum_history(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.45}, created_at=days_ago(3))
        add_assessment_result(db, profile, {"SQL": 0.46}, created_at=days_ago(2))
        # Two flat results are not yet evidence that practice is not working.
        assert not _of(_detect(db, profile), "PRACTICE_WITHOUT_IMPROVEMENT")

    def test_it_does_not_fire_on_an_improving_series(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        for index, score in enumerate([0.3, 0.5, 0.7]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(10 - index))
        assert not _of(_detect(db, profile), "PRACTICE_WITHOUT_IMPROVEMENT")

    def test_it_does_not_fire_on_a_flat_strong_series(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        for index, score in enumerate([0.92, 0.94, 0.91]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(10 - index))
        assert not _of(_detect(db, profile), "PRACTICE_WITHOUT_IMPROVEMENT")

    def test_a_precomputed_series_gives_the_same_answer(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        for index, score in enumerate([0.45, 0.47, 0.44]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(10 - index))

        bundle = signals.collect(db, profile, now=NOW)
        series = diminishing_returns.analyse(bundle)
        assert [c.model_dump() for c in contradictions.detect(bundle, series)] == [
            c.model_dump() for c in contradictions.detect(bundle)
        ]


class TestMatchHighReadinessUnknown:
    def test_it_fires_on_a_strong_match_with_unjudgeable_readiness(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, title="Data Engineer", score=0.85, listing_id_seed=1)

        found = _of(_detect(db, profile), "MATCH_HIGH_READINESS_UNKNOWN")[0]
        assert "strong match" in found.left_statement
        assert found.reclassify_to == "PROFILE_GAP"

    def test_it_never_invents_a_readiness_number(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, score=0.85, listing_id_seed=2)
        found = _of(_detect(db, profile), "MATCH_HIGH_READINESS_UNKNOWN")[0]
        # The whole point is that readiness is unknown; producing a figure would destroy the
        # fact being reported.
        assert "cannot be assessed" in found.explanation

    def test_it_does_not_fire_when_readiness_inputs_are_present(self, db):
        profile = make_profile(db)
        add_resume(db, profile, skills=["sql"])
        add_interview_evaluation(db, profile)
        add_tracked_job(db, profile, score=0.85, listing_id_seed=3)
        assert not _of(_detect(db, profile), "MATCH_HIGH_READINESS_UNKNOWN")

    def test_it_does_not_fire_on_a_weak_match(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, score=0.3, listing_id_seed=4)
        assert not _of(_detect(db, profile), "MATCH_HIGH_READINESS_UNKNOWN")

    def test_it_does_not_fire_with_no_tracked_jobs(self, db):
        profile = make_profile(db)
        assert not _of(_detect(db, profile), "MATCH_HIGH_READINESS_UNKNOWN")


class TestLearningWithoutEvidence:
    def test_it_fires_when_completed_learning_produced_no_proof(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed"), ("SQL", "completed")])

        found = _of(_detect(db, profile), "LEARNING_WITHOUT_EVIDENCE")[0]
        assert found.subject == "SQL"
        assert found.reclassify_to == "EVIDENCE_GAP"
        assert "without converting it into proof" in found.explanation

    def test_it_does_not_blame_the_student_for_the_time_spent(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed")])
        explanation = _of(_detect(db, profile), "LEARNING_WITHOUT_EVIDENCE")[0].explanation
        # The work counts; it just has not been converted yet.
        assert "already done" in explanation
        for blame in ("wasted", "pointless", "useless", "failed"):
            assert blame not in explanation.lower()

    def test_a_self_asserted_claim_does_not_count_as_proof(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed")])
        add_resume(db, profile, skills=["sql"])
        # Writing it on the resume is not converting learning into evidence.
        assert _of(_detect(db, profile), "LEARNING_WITHOUT_EVIDENCE")

    def test_it_stops_firing_once_real_evidence_exists(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed")])
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        assert not _of(_detect(db, profile), "LEARNING_WITHOUT_EVIDENCE")

    def test_it_does_not_fire_on_unfinished_learning(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed"), ("SQL", "not_started"), ("SQL", "not_started")])
        assert not _of(_detect(db, profile), "LEARNING_WITHOUT_EVIDENCE")

    def test_it_is_mutually_exclusive_with_learning_ahead_of_performance(self, db):
        """A measurement is itself independent evidence, so a skill can never satisfy both.
        Asserted directly because the two would otherwise give contradictory advice about the
        same skill on the same screen."""
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed")])
        add_assessment_result(db, profile, {"SQL": 0.3}, created_at=days_ago(1))

        found = _detect(db, profile)
        assert _of(found, "LEARNING_AHEAD_OF_PERFORMANCE")
        assert not _of(found, "LEARNING_WITHOUT_EVIDENCE")


class TestApplicationWithoutProgression:
    def test_it_fires_when_applications_are_not_converting(self, db):
        profile = make_profile(db)
        for seed in range(1, 5):
            add_tracked_job(db, profile, listing_id_seed=seed, status="applied", applied=True)

        found = _of(_detect(db, profile), "APPLICATION_WITHOUT_PROGRESSION")[0]
        assert "4 applications submitted" in found.left_statement
        assert "application activity is high, but progression is weak" in found.explanation

    def test_it_says_may_indicate_rather_than_asserting_a_cause(self, db):
        profile = make_profile(db)
        for seed in range(1, 5):
            add_tracked_job(db, profile, listing_id_seed=seed, status="applied", applied=True)
        explanation = _of(_detect(db, profile), "APPLICATION_WITHOUT_PROGRESSION")[0].explanation
        assert "may indicate" in explanation
        assert "because" not in explanation.lower()

    def test_it_does_not_fire_below_the_readable_application_floor(self, db):
        profile = make_profile(db)
        for seed in range(1, 3):
            add_tracked_job(db, profile, listing_id_seed=seed, status="applied", applied=True)
        # Two applications and no reply yet is a normal wait, not a funnel problem.
        assert not _of(_detect(db, profile), "APPLICATION_WITHOUT_PROGRESSION")

    def test_it_does_not_fire_on_a_converting_funnel(self, db):
        profile = make_profile(db)
        for seed in range(1, 4):
            add_tracked_job(db, profile, listing_id_seed=seed, status="interview", applied=True)
        assert not _of(_detect(db, profile), "APPLICATION_WITHOUT_PROGRESSION")

    def test_saved_but_never_applied_is_not_this_contradiction(self, db):
        profile = make_profile(db)
        for seed in range(1, 6):
            add_tracked_job(db, profile, listing_id_seed=seed)
        # Nothing was applied to, so nothing failed to progress. That is an APPLICATION_GAP.
        assert not _of(_detect(db, profile), "APPLICATION_WITHOUT_PROGRESSION")


class TestNothingFiresFromAbsence:
    def test_an_empty_profile_produces_no_contradictions_at_all(self, db):
        profile = make_profile(db)
        assert _detect(db, profile) == ()

    def test_a_role_with_no_data_produces_no_contradictions(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 0.8, "Spark": 0.8})
        profile = make_profile(db, target_role_id=role.id)
        # Three unevidenced skills are three gaps, not a contradiction -- nothing disagrees.
        assert _detect(db, profile) == ()


class TestContractIntegrity:
    def _busy_profile(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 0.8, "Spark": 0.8})
        profile = make_profile(db, target_role_id=role.id)
        resume = add_resume(db, profile, skills=["aws", "python", "figma", "excel", "tableau", "sql"])
        add_ats_check(db, profile, resume, overall=42)
        add_learning_topics(db, profile, [("SQL", "completed")])
        for index, score in enumerate([0.45, 0.47, 0.44]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(10 - index))
        add_project_build(db, profile, title="Figma Design System", stack_summary="Figma")
        add_interview_evaluation(db, profile, clarity=0.3)
        add_tracked_job(db, profile, score=0.9, missing_skills=["Kafka"], listing_id_seed=5)
        return profile

    def test_every_kind_emitted_is_declared(self, db):
        profile = self._busy_profile(db)
        found = _detect(db, profile)
        assert found
        for item in found:
            assert item.kind in contradictions.CONTRADICTION_KINDS
            assert item.left_statement and item.right_statement
            assert item.explanation
            assert item.left_signal_keys and item.right_signal_keys

    def test_every_reclassification_names_a_real_gap_category(self, db):
        profile = self._busy_profile(db)
        for item in _detect(db, profile):
            if item.reclassify_to is not None:
                assert item.reclassify_to in gaps.GAP_CATEGORIES

    def test_a_contradiction_cannot_be_mutated(self, db):
        profile = self._busy_profile(db)
        item = _detect(db, profile)[0]
        with pytest.raises(ValidationError):
            item.kind = "SOMETHING_ELSE"

    def test_no_explanation_claims_a_hiring_outcome(self, db):
        profile = self._busy_profile(db)
        for item in _detect(db, profile):
            lowered = item.explanation.lower()
            for banned in ("chance of getting hired", "probability", "guarantee", "will get you hired"):
                assert banned not in lowered


class TestDeterminism:
    def test_the_same_bundle_detects_identically(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 0.8, "Spark": 0.8})
        profile = make_profile(db, target_role_id=role.id)
        resume = add_resume(db, profile, skills=["aws", "python", "figma", "excel", "tableau"])
        add_ats_check(db, profile, resume, overall=40)
        add_project_build(db, profile, title="Figma Design System", stack_summary="Figma")
        add_interview_evaluation(db, profile, clarity=0.3)
        add_tracked_job(db, profile, score=0.9, listing_id_seed=6)

        bundle = signals.collect(db, profile, now=NOW)
        first = [c.model_dump() for c in contradictions.detect(bundle)]
        second = [c.model_dump() for c in contradictions.detect(bundle)]
        assert first == second
        assert len(first) > 1  # the ordering guarantee is only meaningful with several

    def test_ordering_follows_the_declared_kind_order(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Kafka": 0.8, "Spark": 0.8})
        profile = make_profile(db, target_role_id=role.id)
        resume = add_resume(db, profile, skills=["aws", "python", "figma", "excel", "tableau"])
        add_ats_check(db, profile, resume, overall=40)
        add_project_build(db, profile, title="Figma Design System", stack_summary="Figma")
        add_tracked_job(db, profile, score=0.9, listing_id_seed=7)

        found = _detect(db, profile)
        order = [contradictions.CONTRADICTION_KINDS.index(c.kind) for c in found]
        assert order == sorted(order)
