"""Gap classification -- the eight categories, and the far more important rules about when
*not* to emit one.

Two properties get the most attention here because getting either wrong sends a student to the
wrong screen for weeks:

  * a skill with real, independent evidence is never classified as something to go *learn*
  * an absent source produces no gap at all, rather than a gap of severity zero
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.services.career_strategy import gaps, signals
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_certification,
    add_education,
    add_experience,
    add_interview_evaluation,
    add_job_preferences,
    add_learning_topics,
    add_project,
    add_project_build,
    add_recruiter_attack,
    add_resume,
    add_saved_search,
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


def _classify(db, profile):
    return gaps.classify(signals.collect(db, profile, now=NOW))


def _for(found, category, subject=None, sub_kind=None):
    return [
        g
        for g in found
        if g.category == category
        and (subject is None or g.subject == subject)
        and (sub_kind is None or g.sub_kind == sub_kind)
    ]


class TestSkillGapKnowledge:
    def test_a_required_skill_with_no_evidence_at_all(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)

        gap = _for(_classify(db, profile), "SKILL_GAP", "SQL", sub_kind="knowledge")[0]
        assert gap.destination == "roadmap"
        assert gap.subject_kind == "skill"
        assert "no evidence of any kind" in gap.reason

    def test_a_core_skill_gap_is_high_severity(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Docker": 0.4})
        profile = make_profile(db, target_role_id=role.id)
        found = _classify(db, profile)

        assert _for(found, "SKILL_GAP", "SQL")[0].severity == "high"
        # A peripheral skill is a real gap but not an urgent one.
        assert _for(found, "SKILL_GAP", "Docker")[0].severity == "low"

    def test_this_is_an_observed_absence_not_missing_data(self, db):
        # The requirement is known, the search ran, the result was nothing. That is different
        # from "we never looked", which is what the non-emission tests below cover.
        role = make_role(db, "SDE", {"Kafka": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        gap = _for(_classify(db, profile), "SKILL_GAP", "Kafka")[0]
        assert gap.signal_keys == ["evidence:Kafka"]


class TestSkillGapAbility:
    def test_a_measured_underperformance_routes_to_practice_not_learning(self, db):
        """The sub_kind exists for exactly this: the product taxonomy has one SKILL_GAP, but
        'never learned it' and 'studied it and still tests at 41%' need different screens."""
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.41}, created_at=days_ago(1))

        gap = _for(_classify(db, profile), "SKILL_GAP", "SQL", sub_kind="ability")[0]
        assert gap.destination == "practice"
        assert "41%" in gap.reason

    def test_a_severely_low_score_is_high_severity(self, db):
        role = make_role(db, "SDE", {"SQL": 0.6})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.2}, created_at=days_ago(1))
        assert _for(_classify(db, profile), "SKILL_GAP", "SQL")[0].severity == "high"

    def test_a_measurement_outranks_a_pile_of_claims(self, db):
        # Precedence in action: observed beats claims, so this stays an ability problem even
        # though the resume and a typed-in project both also mention the skill.
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["sql"])
        add_project(db, profile, title="SQL Reports", tech_stack="SQL")
        add_assessment_result(db, profile, {"SQL": 0.35}, created_at=days_ago(1))

        found = _classify(db, profile)
        assert _for(found, "SKILL_GAP", "SQL", sub_kind="ability")
        assert not _for(found, "EVIDENCE_GAP", "SQL")
        assert not _for(found, "SKILL_GAP", "SQL", sub_kind="knowledge")

    def test_the_worst_measurement_wins_rather_than_an_invented_average(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.30}, created_at=days_ago(1))
        gap = _for(_classify(db, profile), "SKILL_GAP", "SQL")[0]
        # 30%, not some blend nobody scored.
        assert "30%" in gap.reason

    def test_an_acceptable_score_is_not_a_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.85}, created_at=days_ago(1))
        assert not _for(_classify(db, profile), "SKILL_GAP", "SQL")

    def test_only_skill_gaps_carry_a_sub_kind(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_interview_evaluation(db, profile, clarity=0.2)
        for gap in _classify(db, profile):
            if gap.category == "SKILL_GAP":
                assert gap.sub_kind in gaps.SKILL_SUB_KINDS
            else:
                assert gap.sub_kind is None


class TestEvidenceGap:
    def test_claims_with_nothing_independent_behind_them(self, db):
        role = make_role(db, "SDE", {"Airflow": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["airflow"])

        gap = _for(_classify(db, profile), "EVIDENCE_GAP", "Airflow")[0]
        assert gap.destination == "project_builder"
        assert "only in what you have told us" in gap.reason

    def test_a_claim_gap_is_distinguishable_from_a_measured_inability(self, db):
        role = make_role(db, "SDE", {"Airflow": 1.0, "SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["airflow"])
        add_assessment_result(db, profile, {"SQL": 0.3}, created_at=days_ago(1))

        found = _classify(db, profile)
        assert _for(found, "EVIDENCE_GAP", "Airflow")[0].destination == "project_builder"
        assert _for(found, "SKILL_GAP", "SQL")[0].destination == "practice"

    def test_evidence_that_has_aged_out_is_a_proof_gap_not_a_learning_one(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(200))

        found = _classify(db, profile)
        assert "not been demonstrated recently" in _for(found, "EVIDENCE_GAP", "SQL")[0].reason
        # They did learn it. Telling them to go learn it again would be wrong.
        assert not _for(found, "SKILL_GAP", "SQL")


class TestResumeGap:
    def test_real_evidence_the_resume_does_not_carry(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(2))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_resume(db, profile, skills=["python"])

        gap = _for(_classify(db, profile), "RESUME_GAP", "SQL")[0]
        assert gap.destination == "resume_ats"
        assert "does not appear on your analysed resume" in gap.reason

    def test_a_skill_with_evidence_is_never_sent_back_to_learning(self, db):
        """Do not recommend learning something the student has already legitimately evidenced
        just because the resume omits it."""
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(2))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_resume(db, profile, skills=["python"])

        found = _classify(db, profile)
        assert not _for(found, "SKILL_GAP", "SQL")
        assert not _for(found, "EVIDENCE_GAP", "SQL")

    def test_strong_evidence_already_on_the_resume_is_not_a_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(2))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_resume(db, profile, skills=["sql"])
        assert not _for(_classify(db, profile), "RESUME_GAP", "SQL")


class TestExperienceGap:
    def test_it_comes_from_the_match_engines_own_verdict(self, db):
        """No years-of-experience figure is derived anywhere. experience_fit is what
        job_hunt/matching already decided, per advert, on its own seniority ladder."""
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_tracked_job(db, profile, components={"experience_fit": 0.2}, listing_id_seed=1)

        gap = _for(_classify(db, profile), "EXPERIENCE_GAP", "experience_fit")[0]
        assert gap.subject_kind == "match_component"
        assert gap.severity == "high"
        assert "above your recorded experience level" in gap.reason
        assert gap.signal_keys == ["match_component:experience_fit"]

    def test_a_good_experience_fit_is_not_a_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_tracked_job(db, profile, components={"experience_fit": 0.9}, listing_id_seed=2)
        assert not _for(_classify(db, profile), "EXPERIENCE_GAP")

    def test_no_tracked_jobs_means_no_experience_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_experience(db, profile, company="Acme")
        # Work history alone says nothing about fit: there is no advert to misfit against.
        assert not _for(_classify(db, profile), "EXPERIENCE_GAP")

    def test_a_component_the_match_engine_could_not_judge_is_not_a_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        # matching omits a component it could not judge; substituting 0.0 would convert "the
        # advert did not say" into "the student does not fit".
        add_tracked_job(db, profile, components={"skill_overlap": 0.8}, listing_id_seed=3)
        assert not _for(_classify(db, profile), "EXPERIENCE_GAP")


class TestApplicationGap:
    def test_saving_jobs_without_applying(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, listing_id_seed=1)
        add_tracked_job(db, profile, listing_id_seed=2)

        gap = _for(_classify(db, profile), "APPLICATION_GAP", "applications")[0]
        assert gap.destination == "tracker"
        assert gap.subject_kind == "funnel"
        assert "no applications submitted yet" in gap.reason

    def test_applying_without_progression(self, db):
        profile = make_profile(db)
        for seed in range(1, 5):
            add_tracked_job(db, profile, listing_id_seed=seed, status="applied", applied=True)

        gap = _for(_classify(db, profile), "APPLICATION_GAP", "progression")[0]
        assert gap.severity == "high"
        assert "progression is not following it" in gap.reason

    def test_it_never_claims_causation(self, db):
        profile = make_profile(db)
        for seed in range(1, 5):
            add_tracked_job(db, profile, listing_id_seed=seed, status="applied", applied=True)
        reason = _for(_classify(db, profile), "APPLICATION_GAP", "progression")[0].reason
        for causal in ("because", "caused by", "due to"):
            assert causal not in reason.lower()

    def test_too_few_applications_is_not_a_funnel_problem(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, listing_id_seed=1, status="applied", applied=True)
        add_tracked_job(db, profile, listing_id_seed=2, status="applied", applied=True)
        # Two applications that have not moved yet is noise, not a pattern.
        assert not _for(_classify(db, profile), "APPLICATION_GAP", "progression")

    def test_a_healthy_funnel_produces_no_gap(self, db):
        profile = make_profile(db)
        for seed in range(1, 4):
            add_tracked_job(db, profile, listing_id_seed=seed, status="interview", applied=True)
        assert not _for(_classify(db, profile), "APPLICATION_GAP")

    def test_an_application_still_counts_after_a_rejection(self, db):
        # applied_at is set once and never rewritten, so a rejected application is still an
        # application. Reading the status string instead would lose it.
        profile = make_profile(db)
        for seed in range(1, 5):
            add_tracked_job(db, profile, listing_id_seed=seed, status="rejected", applied=True)
        assert _for(_classify(db, profile), "APPLICATION_GAP", "progression")

    def test_no_tracked_jobs_means_no_application_gap(self, db):
        profile = make_profile(db)
        assert not _for(_classify(db, profile), "APPLICATION_GAP")


class TestInterviewGap:
    def test_a_weak_dimension_from_a_single_evaluation(self, db):
        profile = make_profile(db)
        add_interview_evaluation(db, profile, clarity=0.3)

        gap = _for(_classify(db, profile), "INTERVIEW_GAP", "interview:clarity")[0]
        assert gap.destination == "interview"
        assert gap.subject_kind == "interview_dimension"
        assert gap.role_weight is None

    def test_interview_memory_outranks_a_single_evaluation(self, db):
        profile = make_profile(db)
        add_interview_evaluation(db, profile, clarity=0.3)
        add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=4)

        found = _for(_classify(db, profile), "INTERVIEW_GAP", "interview:clarity")
        assert len(found) == 1
        assert "4 times" in found[0].reason
        assert found[0].severity == "high"

    def test_a_closed_weakness_no_longer_produces_a_gap(self, db):
        profile = make_profile(db)
        add_interview_evaluation(db, profile, clarity=0.3)
        add_weakness_record(db, profile, "interview:clarity", status="CLOSED", occurrences=3, last_score=0.9)
        assert not _for(_classify(db, profile), "INTERVIEW_GAP", "interview:clarity")

    def test_severity_comes_from_the_record_not_from_its_display_label(self, db):
        """Regression: status and occurrences are data, so they are read off the WeaknessRecord
        row. An earlier version recovered them by splitting the signal's human-readable
        raw_label, which meant a copy-edit to that wording could silently flip a severity."""
        profile = make_profile(db)
        add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=3)

        bundle = signals.collect(db, profile, now=NOW)
        reworded = bundle.__class__(
            **{
                **{name: getattr(bundle, name) for name in bundle.__dataclass_fields__},
                "signals": tuple(
                    s.model_copy(update={"raw_label": "completely different wording"})
                    for s in bundle.signals
                ),
            }
        )

        def severities(found):
            return {(g.subject, g.severity) for g in _for(found, "INTERVIEW_GAP")}

        assert severities(gaps.classify(reworded)) == severities(gaps.classify(bundle))
        assert severities(gaps.classify(bundle)) == {("interview:clarity", "high")}

    def test_a_recruiter_attack_dimension_can_also_raise_one(self, db):
        profile = make_profile(db)
        add_recruiter_attack(db, profile, specificity=0.25)
        assert _for(_classify(db, profile), "INTERVIEW_GAP", "recruiter_attack:specificity")

    def test_a_passing_dimension_raises_nothing(self, db):
        profile = make_profile(db)
        add_interview_evaluation(db, profile)
        assert not _for(_classify(db, profile), "INTERVIEW_GAP")


class TestInterviewDimensionsStayOutOfSkillLogic:
    def test_no_interview_gap_is_ever_typed_as_a_skill(self, db):
        profile = make_profile(db)
        add_interview_evaluation(db, profile, clarity=0.2, structure=0.3)
        add_weakness_record(db, profile, "interview:reasoning")
        for gap in _for(_classify(db, profile), "INTERVIEW_GAP"):
            assert gap.subject_kind == "interview_dimension"
            assert gap.role_weight is None

    def test_no_skill_typed_gap_has_a_namespaced_subject(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_interview_evaluation(db, profile, clarity=0.2, structure=0.25)
        for gap in _classify(db, profile):
            if gap.subject_kind == "skill":
                assert not signals.is_interview_subject(gap.subject)


class TestJobTargetingGap:
    def test_a_skill_the_tracked_jobs_want_that_the_role_does_not(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_tracked_job(db, profile, missing_skills=["Kafka"], listing_id_seed=1)

        gap = _for(_classify(db, profile), "JOB_TARGETING_GAP", "Kafka")[0]
        assert gap.destination == "job_hunt"
        assert gap.job_demand_share == pytest.approx(1.0)

    def test_low_role_alignment_is_its_own_targeting_signal(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_tracked_job(db, profile, components={"role_alignment": 0.25}, listing_id_seed=2)

        gap = _for(_classify(db, profile), "JOB_TARGETING_GAP", "role_alignment")[0]
        assert gap.subject_kind == "match_component"
        assert gap.severity == "high"

    def test_it_comes_only_from_real_tracked_listings(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        # No tracked jobs at all: there is no such thing as generic market demand here.
        assert not _for(_classify(db, profile), "JOB_TARGETING_GAP")

    def test_a_stale_match_version_produces_no_targeting_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_tracked_job(db, profile, missing_skills=["Kafka"], score_version="v0-old", listing_id_seed=3)
        assert not _for(_classify(db, profile), "JOB_TARGETING_GAP")

    def test_a_role_skill_is_not_double_counted(self, db):
        role = make_role(db, "SDE", {"Kafka": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_tracked_job(db, profile, missing_skills=["Kafka"], listing_id_seed=4)

        found = _classify(db, profile)
        assert not _for(found, "JOB_TARGETING_GAP", "Kafka")
        assert _for(found, "SKILL_GAP", "Kafka")

    def test_job_demand_raises_the_severity_of_a_role_skill_gap(self, db):
        role = make_role(db, "SDE", {"Kafka": 0.4})
        profile = make_profile(db, target_role_id=role.id)
        add_tracked_job(db, profile, missing_skills=["Kafka"], listing_id_seed=5)
        gap = _for(_classify(db, profile), "SKILL_GAP", "Kafka")[0]
        # Peripheral to the role, but every tracked job wants it.
        assert gap.severity == "high"
        assert gap.job_demand_share == pytest.approx(1.0)


class TestProfileGap:
    def test_a_missing_target_role_is_the_one_high_severity_profile_gap(self, db):
        profile = make_profile(db)
        gap = _for(_classify(db, profile), "PROFILE_GAP", "target_role")[0]
        assert gap.severity == "high"
        assert gap.destination == "career_path"

    def test_missing_readiness_inputs_are_reported_with_their_own_destinations(self, db):
        profile = make_profile(db)
        found = _for(_classify(db, profile), "PROFILE_GAP")
        by_subject = {g.subject: g for g in found}
        assert by_subject["resume"].destination == "resume_ats"
        assert by_subject["interview"].destination == "interview"

    def test_they_disappear_once_the_inputs_exist(self, db):
        profile = make_profile(db)
        add_resume(db, profile, skills=["sql"])
        add_interview_evaluation(db, profile)
        subjects = {g.subject for g in _for(_classify(db, profile), "PROFILE_GAP")}
        assert "resume" not in subjects
        assert "interview" not in subjects

    def test_no_readiness_number_is_computed_anywhere(self, db):
        # readiness.compute stays the sole owner; this layer only says what cannot be judged.
        profile = make_profile(db)
        for gap in _for(_classify(db, profile), "PROFILE_GAP"):
            if gap.subject in ("resume", "interview"):
                assert "%" not in gap.reason

    def test_missing_profile_fields_are_low_severity(self, db):
        profile = make_profile(db, degree="B.Tech")
        found = _for(_classify(db, profile), "PROFILE_GAP")
        assert all(g.severity == "low" for g in found if g.subject not in ("target_role", "resume", "interview"))
        assert not [g for g in found if g.subject == "degree"]

    def test_each_absence_is_reported_exactly_once(self, db):
        profile = make_profile(db)
        subjects = [g.subject for g in _for(_classify(db, profile), "PROFILE_GAP")]
        assert len(subjects) == len(set(subjects))


class TestNonEmission:
    """Missing data stays missing. These are the tests that stop the engine inventing work."""

    def test_no_target_role_produces_no_skill_gaps_at_all(self, db):
        profile = make_profile(db)
        add_resume(db, profile, skills=["sql", "python"])
        found = _classify(db, profile)
        assert not [g for g in found if g.category in ("SKILL_GAP", "EVIDENCE_GAP", "RESUME_GAP")]

    def test_no_resume_means_no_resume_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(2))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        assert not _for(_classify(db, profile), "RESUME_GAP")

    def test_an_unparsed_resume_also_means_no_resume_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(2))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_resume(db, profile, skills=["python"], parse_status="pending")
        assert not _for(_classify(db, profile), "RESUME_GAP")

    def test_no_interviews_means_no_interview_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        assert not _for(_classify(db, profile), "INTERVIEW_GAP")

    def test_a_resume_skill_outside_the_role_is_not_a_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["sql", "figma"])
        assert not [g for g in _classify(db, profile) if g.subject == "figma"]

    def test_a_fully_evidenced_profile_produces_no_skill_gaps(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.92}, created_at=days_ago(2))
        add_project_build(db, profile, title="SQL Pipeline", stack_summary="SQL")
        add_resume(db, profile, skills=["sql"])
        assert not [g for g in _classify(db, profile) if g.subject_kind == "skill"]


class TestContractIntegrity:
    def test_every_category_and_destination_is_declared(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Airflow": 0.8, "Docker": 0.4})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["airflow"])
        add_assessment_result(db, profile, {"SQL": 0.3}, created_at=days_ago(1))
        add_interview_evaluation(db, profile, clarity=0.2)
        add_tracked_job(db, profile, missing_skills=["Kafka"], listing_id_seed=6)

        found = _classify(db, profile)
        assert found
        for gap in found:
            assert gap.category in gaps.GAP_CATEGORIES
            assert gap.destination in gaps.DESTINATIONS
            assert gap.subject_kind in gaps.SUBJECT_KINDS
            assert gap.severity in ("low", "medium", "high")
            assert gap.reason
            assert gap.signal_keys

    def test_all_eight_categories_are_reachable(self, db):
        """One profile arranged to trigger every category at once, proving none is dead code."""
        role = make_role(db, "SDE", {"SQL": 1.0, "Airflow": 0.8, "Redis": 0.8, "Spark": 0.8})
        profile = make_profile(db, target_role_id=role.id)
        # SKILL_GAP (knowledge) -> Spark; SKILL_GAP (ability) -> SQL
        add_assessment_result(db, profile, {"SQL": 0.3}, created_at=days_ago(1))
        # EVIDENCE_GAP -> Airflow (claim only); RESUME_GAP -> Redis (evidenced, off-resume)
        add_assessment_result(db, profile, {"Redis": 0.9}, created_at=days_ago(2))
        add_project_build(db, profile, title="Redis Cache", stack_summary="Redis")
        add_resume(db, profile, skills=["airflow", "sql"])
        # INTERVIEW_GAP -> clarity
        add_interview_evaluation(db, profile, clarity=0.2)
        # JOB_TARGETING_GAP -> Kafka; EXPERIENCE_GAP -> weak experience_fit
        # APPLICATION_GAP -> tracked but never applied
        add_tracked_job(
            db, profile, missing_skills=["Kafka"],
            components={"experience_fit": 0.2, "role_alignment": 0.8}, listing_id_seed=7,
        )
        # PROFILE_GAP -> degree/branch never filled in

        present = {gap.category for gap in _classify(db, profile)}
        assert present == set(gaps.GAP_CATEGORIES), f"missing: {set(gaps.GAP_CATEGORIES) - present}"

    def test_a_gap_cannot_be_mutated(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        gap = _classify(db, profile)[0]
        with pytest.raises(ValidationError):
            gap.severity = "low"

    def test_learning_progress_does_not_by_itself_create_a_gap(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_learning_topics(db, profile, [("SQL", "completed")])
        assert len(_for(_classify(db, profile), "SKILL_GAP", "SQL")) == 1

    def test_preferences_and_background_never_become_gaps_of_their_own(self, db):
        """Preferred employers and relocation are preferences, not capability. They may surface
        as a reason or break a tie; they must never become a gap or a hidden score."""
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_job_preferences(db, profile, preferred_companies=["Acme"], open_to_relocation=True)
        add_saved_search(db, profile)
        add_certification(db, profile)
        add_education(db, profile)
        subjects = {g.subject for g in _classify(db, profile)}
        assert "preferred_employers" not in subjects
        assert "relocation" not in subjects


class TestDeterminism:
    def _profile(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Airflow": 0.8, "Docker": 0.4})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["airflow", "figma"])
        add_assessment_result(db, profile, {"SQL": 0.3}, created_at=days_ago(1))
        add_interview_evaluation(db, profile, clarity=0.2)
        add_tracked_job(db, profile, missing_skills=["Kafka", "Spark"], listing_id_seed=8)
        add_job_preferences(db, profile, preferred_companies=["Acme"])
        return profile

    def test_the_same_bundle_classifies_identically(self, db):
        profile = self._profile(db)
        bundle = signals.collect(db, profile, now=NOW)
        first = [g.model_dump() for g in gaps.classify(bundle)]
        second = [g.model_dump() for g in gaps.classify(bundle)]
        assert first == second

    def test_two_collections_of_the_same_data_classify_identically(self, db):
        profile = self._profile(db)
        assert [g.model_dump() for g in _classify(db, profile)] == [
            g.model_dump() for g in _classify(db, profile)
        ]

    def test_ordering_is_severity_first(self, db):
        profile = self._profile(db)
        ranks = [gaps._SEVERITY_RANK[g.severity] for g in _classify(db, profile)]
        assert ranks == sorted(ranks, reverse=True)
