"""Signal collection -- every source, and every way a source can be absent.

The recurring assertion in this file is that an absent source produces `value=None` with
`evidence_kind="missing"` and never a zero. That is the single property most likely to be
broken by a well-meaning refactor, and the one whose breakage is least visible: a 0.0 flows
silently into an average and tells a student something we never measured.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.services.career_strategy import precedence, signals
from app.services.job_hunt import matching
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_ats_check,
    add_interview_evaluation,
    add_learning_topics,
    add_mission,
    add_project,
    add_recruiter_attack,
    add_resume,
    add_tracked_job,
    add_weakness_record,
    days_ago,
    make_career_path,
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


def _keys(bundle):
    return {signal.key for signal in bundle.signals}


class TestAnEmptyProfile:
    """A brand-new account. Every source is absent, and none of them may become a zero."""

    def test_no_target_role_is_a_missing_signal_not_an_empty_success(self, db):
        profile = make_profile(db)
        bundle = signals.collect(db, profile, now=NOW)
        role_signal = bundle.by_key("career:target_role")
        assert role_signal.evidence_kind == "missing"
        assert bundle.role is None
        assert bundle.skills == ()

    def test_no_source_produces_a_zero_value(self, db):
        profile = make_profile(db)
        bundle = signals.collect(db, profile, now=NOW)
        for signal in bundle.signals:
            if signal.evidence_kind == "missing":
                assert signal.value is None, f"{signal.key} turned a missing source into a number"

    def test_absent_sources_are_still_reported_rather_than_omitted(self, db):
        # "We looked and there is nothing" has to be expressible; a silently absent key is
        # indistinguishable from a collector that failed to run.
        profile = make_profile(db)
        keys = _keys(signals.collect(db, profile, now=NOW))
        assert {"resume:on_file", "ats:overall", "interview:history", "job:tracked_count"} <= keys

    def test_no_resume_analysis_is_none_not_an_empty_list(self, db):
        profile = make_profile(db)
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.resume_skills is None
        assert bundle.has_resume is False


class TestEvidenceSignals:
    def test_a_role_skill_with_nothing_behind_it_is_missing(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        bundle = signals.collect(db, profile, now=NOW)

        signal = bundle.by_key("evidence:SQL")
        assert signal.evidence_kind == "missing"
        # evidence_engine returns confidence 0.0 for its `claimed` state; carrying that through
        # as a number would say "we measured your confidence at zero".
        assert signal.value is None
        assert signal.confidence is None

    def test_a_measured_assessment_becomes_an_observed_signal(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.82}, created_at=days_ago(2))

        bundle = signals.collect(db, profile, now=NOW)
        measured = bundle.by_key("assessment:SQL")
        assert measured.evidence_kind == "observed"
        assert measured.value == pytest.approx(0.82, abs=0.01)
        assert measured.category == "technical_performance"

    def test_a_resume_mention_is_a_resume_claim_not_a_measurement(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["sql"])

        bundle = signals.collect(db, profile, now=NOW)
        claim = bundle.by_key("resume:SQL")
        assert claim.evidence_kind == "resume_claim"
        assert precedence.is_claim(claim.evidence_kind) is True
        assert claim.value is None

    def test_a_typed_in_project_is_a_user_claim(self, db):
        role = make_role(db, "SDE", {"Airflow": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_project(db, profile, title="Pipeline", tech_stack="Airflow")

        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("project:Airflow").evidence_kind == "user_claim"

    def test_confidence_is_carried_only_where_evidence_engine_publishes_one(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(1))

        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("evidence:SQL").confidence is not None
        # No other collector invents one to fill the field.
        assert bundle.by_key("assessment:SQL").confidence is None
        assert bundle.by_key("career:target_role").confidence is None

    def test_the_rollup_is_marked_derived_and_the_measurement_direct(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(1))

        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("evidence:SQL").is_direct is False
        assert bundle.by_key("assessment:SQL").is_direct is True

    def test_stale_evidence_stays_observed_and_carries_its_age(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(200))

        bundle = signals.collect(db, profile, now=NOW)
        rollup = bundle.by_key("evidence:SQL")
        # Aged out, but it did happen -- demoting it to "missing" would erase real history.
        assert rollup.evidence_kind == "observed"
        assert bundle.evidence_by_skill["SQL"].state == "stale"
        assert bundle.by_key("assessment:SQL").freshness_days >= 199

    def test_undated_evidence_has_no_fabricated_freshness(self, db):
        role = make_role(db, "SDE", {"Airflow": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_project(db, profile, title="Pipeline", tech_stack="Airflow")

        bundle = signals.collect(db, profile, now=NOW)
        # A typed-in project row has no timestamp of its own. Inventing one would let it age.
        assert bundle.by_key("project:Airflow").observed_at is None
        assert bundle.by_key("project:Airflow").freshness_days is None

    def test_every_signal_carries_its_provenance(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.9}, created_at=days_ago(1))

        bundle = signals.collect(db, profile, now=NOW)
        for signal in bundle.signals:
            assert signal.provenance, f"{signal.key} has no provenance"


class TestConflictingSources:
    def test_a_claim_and_a_measurement_are_both_kept(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["sql"])
        add_assessment_result(db, profile, {"SQL": 0.3}, created_at=days_ago(1))

        bundle = signals.collect(db, profile, now=NOW)
        # Neither overwrites the other; precedence decides which to act on, later.
        assert bundle.by_key("resume:SQL").evidence_kind == "resume_claim"
        assert bundle.by_key("assessment:SQL").evidence_kind == "observed"

    def test_precedence_over_the_collected_kinds_picks_the_measurement(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["sql"])
        add_assessment_result(db, profile, {"SQL": 0.3}, created_at=days_ago(1))

        bundle = signals.collect(db, profile, now=NOW)
        pairs = [(s.key, s.evidence_kind) for s in bundle.for_subject("SQL") if s.key != "evidence:SQL"]
        resolution = precedence.resolve("SQL", pairs)
        assert resolution.winning_kind == "observed"
        assert "resume:SQL" in resolution.overridden_keys


class TestPartialSources:
    def test_an_uploaded_but_unparsed_resume_is_not_treated_as_analysed(self, db):
        profile = make_profile(db)
        add_resume(db, profile, skills=["sql"], parse_status="pending")

        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.has_resume is True
        assert bundle.resume_skills is None
        assert bundle.by_key("resume:on_file").evidence_kind == "missing"
        assert "not available yet" in bundle.by_key("resume:on_file").raw_label

    def test_a_voice_only_interview_dimension_is_skipped_rather_than_defaulted(self, db):
        profile = make_profile(db)
        add_interview_evaluation(db, profile)
        bundle = signals.collect(db, profile, now=NOW)
        # speaking_pace/filler_word_rate are null for a text interview.
        assert bundle.by_key("interview:speaking_pace") is None
        assert bundle.by_key("interview:clarity") is not None

    def test_a_tracked_job_without_a_match_row_still_counts_as_tracked(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, score_version="v0-old", listing_id_seed=7)
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("job:tracked_count").evidence_kind == "observed"
        assert bundle.tracked_jobs[0][1] is None


class TestJobSignals:
    def test_demand_comes_only_from_real_tracked_matches(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, missing_skills=["Kafka"], score=0.8, listing_id_seed=1)
        bundle = signals.collect(db, profile, now=NOW)

        demand = bundle.by_key("job_demand:Kafka")
        assert demand.value == pytest.approx(1.0)
        assert "your 1 scored tracked jobs" in demand.raw_label

    def test_a_superseded_score_version_is_ignored_not_migrated(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, missing_skills=["Kafka"], score_version="v0-old", listing_id_seed=2)
        bundle = signals.collect(db, profile, now=NOW)
        # A stale score is not authoritative; treating it as current would assert something the
        # match engine no longer stands behind.
        assert bundle.by_key("job_demand:Kafka") is None

    def test_the_current_score_version_is_read_from_the_match_engine(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, score=0.8, listing_id_seed=3)
        bundle = signals.collect(db, profile, now=NOW)
        match_signal = next(s for s in bundle.signals if s.key.startswith("job_match:"))
        assert matching.SCORE_VERSION in match_signal.provenance

    def test_no_tracked_jobs_means_no_demand_claim_at_all(self, db):
        profile = make_profile(db)
        bundle = signals.collect(db, profile, now=NOW)
        assert not [s for s in bundle.signals if s.key.startswith("job_demand:")]
        assert bundle.by_key("job:tracked_count").evidence_kind == "missing"

    def test_the_match_score_is_carried_through_unchanged(self, db):
        profile = make_profile(db)
        add_tracked_job(db, profile, score=0.73, listing_id_seed=4)
        bundle = signals.collect(db, profile, now=NOW)
        match_signal = next(s for s in bundle.signals if s.key.startswith("job_match:"))
        # Never recomputed here -- read straight off the authoritative row.
        assert match_signal.value == pytest.approx(0.73)


class TestOtherCollectors:
    def test_learning_progress_merges_both_roadmap_systems(self, db):
        profile = make_profile(db)
        add_learning_topics(db, profile, [("SQL", "completed"), ("SQL", "not_started")])
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("learning:SQL").value == pytest.approx(0.5)

    def test_a_skill_on_no_roadmap_gets_no_learning_signal(self, db):
        profile = make_profile(db)
        add_learning_topics(db, profile, [("SQL", "completed")])
        bundle = signals.collect(db, profile, now=NOW)
        # Absence of a plan is not zero progress.
        assert bundle.by_key("learning:Kafka") is None

    def test_ats_scores_are_normalised_to_the_shared_scale(self, db):
        profile = make_profile(db)
        resume = add_resume(db, profile, skills=["sql"])
        add_ats_check(db, profile, resume, overall=64, skills=40)
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("ats:overall").value == pytest.approx(0.64)
        assert bundle.by_key("ats:skills").value == pytest.approx(0.40)

    def test_interview_memory_carries_status_and_occurrences(self, db):
        profile = make_profile(db)
        add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=3, last_score=0.35)
        bundle = signals.collect(db, profile, now=NOW)
        signal = bundle.by_key("weakness_memory:interview:clarity")
        assert "REPEATED" in signal.raw_label
        assert "seen 3x" in signal.raw_label
        assert signal.value == pytest.approx(0.35)

    def test_recruiter_attack_dimensions_are_collected(self, db):
        profile = make_profile(db)
        add_recruiter_attack(db, profile, communication=0.3)
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("recruiter_attack:communication").value == pytest.approx(0.3)

    def test_readiness_inputs_are_reported_without_computing_readiness(self, db):
        profile = make_profile(db)
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("readiness_input:resume").evidence_kind == "missing"
        assert bundle.by_key("readiness_input:interview").evidence_kind == "missing"
        # No readiness number is produced anywhere -- readiness.compute stays the only owner.
        assert not [s for s in bundle.signals if s.key.startswith("readiness:")]

    def test_missing_profile_fields_are_marked_missing(self, db):
        profile = make_profile(db, degree="B.Tech")
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("profile:degree").evidence_kind == "observed"
        assert bundle.by_key("profile:branch").evidence_kind == "missing"

    def test_mission_activity_is_counted(self, db):
        profile = make_profile(db)
        add_mission(db, profile, status="completed", days=1)
        add_mission(db, profile, status="not_started", days=2)
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("activity:missions_completed").raw_label == "1 of 2 missions completed"

    def test_an_ai_generated_role_is_flagged_as_an_estimate(self, db):
        profile = make_profile(db)
        make_career_path(db, profile, core_skills=["Prompt Design"], via="ai_generated")
        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.by_key("career:target_role").is_direct is False


class TestTheSkillUniverse:
    def test_role_skills_come_first_then_extra_resume_skills(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_resume(db, profile, skills=["sql", "figma"])

        bundle = signals.collect(db, profile, now=NOW)
        assert bundle.skills[0] == "SQL"
        # "sql" is the same skill as "SQL" and must not be added twice.
        assert list(bundle.skills).count("SQL") == 1
        assert "figma" in bundle.skills

    def test_interview_dimensions_never_enter_the_skill_universe(self, db):
        profile = make_profile(db)
        add_interview_evaluation(db, profile, clarity=0.2)
        add_weakness_record(db, profile, "interview:clarity")
        bundle = signals.collect(db, profile, now=NOW)
        assert not any(signals.is_interview_subject(skill) for skill in bundle.skills)

    @pytest.mark.parametrize(
        "subject,expected",
        [
            ("interview:clarity", True),
            ("recruiter_attack:specificity", True),
            ("SQL", False),
            ("Interviewing", False),
            (None, False),
        ],
    )
    def test_the_interview_subject_predicate(self, subject, expected):
        assert signals.is_interview_subject(subject) is expected

    def test_an_unmeasured_dimension_is_neither_weak_nor_fine(self, db):
        # Tri-state on purpose: False would claim we checked.
        assert signals.interview_dimension_is_weak(None) is None
        assert signals.interview_dimension_is_weak(0.3) is True
        assert signals.interview_dimension_is_weak(0.9) is False


class TestContractIntegrity:
    def test_every_signal_uses_a_declared_category_and_kind(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Python": 0.7})
        profile = make_profile(db, target_role_id=role.id)
        resume = add_resume(db, profile, skills=["sql", "figma"])
        add_ats_check(db, profile, resume)
        add_assessment_result(db, profile, {"SQL": 0.4}, priority_weaknesses=["SQL"], created_at=days_ago(1))
        add_interview_evaluation(db, profile, clarity=0.3)
        add_recruiter_attack(db, profile)
        add_learning_topics(db, profile, [("SQL", "completed")])
        add_tracked_job(db, profile, missing_skills=["Kafka"], listing_id_seed=5)
        add_weakness_record(db, profile, "interview:clarity")
        add_mission(db, profile)

        bundle = signals.collect(db, profile, now=NOW)
        assert len(bundle.signals) > 20
        for signal in bundle.signals:
            assert signal.category in signals.SIGNAL_CATEGORIES
            assert signal.evidence_kind in precedence.EVIDENCE_KINDS

    def test_a_signal_cannot_be_mutated(self, db):
        profile = make_profile(db)
        bundle = signals.collect(db, profile, now=NOW)
        with pytest.raises(ValidationError):
            bundle.signals[0].value = 1.0

    def test_an_undeclared_category_is_rejected_at_construction(self):
        with pytest.raises(ValueError):
            signals._signal(key="x", category="not_a_category", source="s", now=NOW)

    def test_an_undeclared_evidence_kind_is_rejected_at_construction(self):
        with pytest.raises(precedence.UnknownEvidenceKindError):
            signals._signal(key="x", category="identity", source="s", now=NOW, evidence_kind="verifed")


class TestDeterminism:
    def _rich_profile(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Python": 0.7, "Kafka": 0.5})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech")
        resume = add_resume(db, profile, skills=["sql", "figma"])
        add_ats_check(db, profile, resume, overall=55)
        add_assessment_result(db, profile, {"SQL": 0.45}, priority_weaknesses=["SQL"], created_at=days_ago(3))
        add_assessment_result(db, profile, {"SQL": 0.47}, created_at=days_ago(2))
        add_interview_evaluation(db, profile, clarity=0.3, structure=0.35)
        add_recruiter_attack(db, profile, communication=0.4)
        add_learning_topics(db, profile, [("SQL", "completed"), ("Python", "not_started")])
        add_tracked_job(db, profile, missing_skills=["Kafka", "Spark"], score=0.82, listing_id_seed=9)
        add_weakness_record(db, profile, "interview:clarity", occurrences=3)
        add_mission(db, profile)
        return profile

    def test_the_same_data_and_the_same_now_produce_identical_signals(self, db):
        profile = self._rich_profile(db)
        first = signals.collect(db, profile, now=NOW)
        second = signals.collect(db, profile, now=NOW)
        assert [s.model_dump() for s in first.signals] == [s.model_dump() for s in second.signals]

    def test_ordering_is_stable_across_calls(self, db):
        profile = self._rich_profile(db)
        first = [s.key for s in signals.collect(db, profile, now=NOW).signals]
        second = [s.key for s in signals.collect(db, profile, now=NOW).signals]
        assert first == second

    def test_the_skill_universe_is_stable_across_calls(self, db):
        profile = self._rich_profile(db)
        assert signals.collect(db, profile, now=NOW).skills == signals.collect(db, profile, now=NOW).skills

    def test_freshness_moves_only_when_now_moves(self, db):
        profile = self._rich_profile(db)
        base = signals.collect(db, profile, now=NOW).by_key("assessment:SQL").freshness_days
        later = signals.collect(
            db, profile, now=NOW + __import__("datetime").timedelta(days=10)
        ).by_key("assessment:SQL").freshness_days
        assert later == base + 10
