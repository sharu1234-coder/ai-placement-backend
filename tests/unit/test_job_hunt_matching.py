"""The match score.

The property under test throughout is the one the engine is built around: *a factor the
provider did not supply must not lower the score*. Adverts routinely omit salary, work mode and
seniority, and scoring an absent field as zero would punish a job for its provider's gaps while
telling the student "you are a poor fit" -- a claim the data never supported.
"""

import datetime

from app.services.job_hunt import matching
from app.services.job_hunt.matching import (
    SCORE_VERSION,
    WEIGHTS,
    MatchInput,
    score_match,
)

_NOW = datetime.datetime(2026, 9, 7, 12, 0, 0)


def make_input(**overrides) -> MatchInput:
    """A well-specified baseline: every factor knowable, everything agreeing.

    job_posted_at is exactly `now` rather than a day ago so the baseline really is a perfect
    1.0. Freshness decays continuously from the moment of posting, so a one-day-old advert
    scores 0.967 on that component -- correct behaviour, but it makes the baseline unusable as
    an "everything perfect" fixture.
    """
    base = dict(
        student_skills=["python", "sql"],
        student_role_family="software_engineer",
        student_experience_level="fresher",
        preferred_location="Bengaluru",
        preferred_work_mode=None,
        preferred_employment_type=None,
        job_title="Software Engineer",
        job_skills=["python", "sql"],
        job_location="Bengaluru, Karnataka",
        job_work_mode=None,
        job_employment_type=None,
        job_seniority="fresher",
        job_posted_at=_NOW,
    )
    base.update(overrides)
    return MatchInput(**base)


class TestBounds:
    def test_a_perfect_match_scores_one_and_a_total_mismatch_scores_zero(self):
        assert score_match(make_input(), now=_NOW).total_score == 1.0

        worst = score_match(
            make_input(
                job_title="Chief Financial Officer",
                student_role_family="data_scientist",
                job_skills=["kubernetes"],
                student_skills=["python"],
                job_seniority="lead",
                job_location="Delhi",
                job_posted_at=_NOW - datetime.timedelta(days=400),
            ),
            now=_NOW,
        )
        assert worst.total_score == 0.0

    def test_every_score_stays_within_zero_and_one(self):
        for job_skills in ([], ["python"], ["python", "sql", "kubernetes", "aws"]):
            result = score_match(make_input(job_skills=job_skills), now=_NOW)
            assert 0.0 <= result.total_score <= 1.0
            for component, value in result.component_scores.items():
                assert 0.0 <= value <= 1.0, f"{component} out of range"

    def test_the_same_inputs_always_produce_the_same_score(self):
        """Reproducibility is a product promise: a score shown to a student has to be
        defensible later, which is only possible if it is deterministic."""
        first = score_match(make_input(), now=_NOW)
        second = score_match(make_input(), now=_NOW)
        assert first.total_score == second.total_score
        assert first.component_scores == second.component_scores

    def test_the_version_is_reported_so_a_stale_stored_score_is_detectable(self):
        assert score_match(make_input(), now=_NOW).score_version == SCORE_VERSION


class TestUnknownIsNotZero:
    def test_a_missing_posted_date_does_not_reduce_the_score(self):
        known = score_match(make_input(), now=_NOW)
        unknown = score_match(make_input(job_posted_at=None), now=_NOW)

        assert "freshness" not in unknown.component_scores
        assert any("posted date" in factor for factor in unknown.unknown_factors)
        # Both are perfect matches on everything knowable, so dropping an unknowable factor
        # must leave the score untouched rather than dragging it down.
        assert unknown.total_score == known.total_score == 1.0

    def test_a_missing_seniority_is_reported_rather_than_scored(self):
        result = score_match(
            make_input(job_title="Software Engineer", job_seniority=None), now=_NOW
        )
        assert "experience_fit" not in result.component_scores
        assert any("seniority" in factor for factor in result.unknown_factors)

    def test_an_advert_naming_no_skills_is_unknown_not_a_zero_overlap(self):
        """A short advert says nothing about the student. Scoring it as 0% overlap would be a
        statement the advert never made."""
        result = score_match(make_input(job_skills=[]), now=_NOW)
        assert "skill_overlap" not in result.component_scores
        assert any("doesn't name specific skills" in f for f in result.unknown_factors)

    def test_a_student_with_no_target_role_gets_an_unknown_not_a_penalty(self):
        result = score_match(make_input(student_role_family=None), now=_NOW)
        assert "role_alignment" not in result.component_scores
        assert any("target role isn't set" in f for f in result.unknown_factors)

    def test_a_wrong_city_is_a_real_zero_because_it_is_genuinely_knowable(self):
        """The counterpart to the tests above: an unknown is excluded, but a *known* mismatch
        must still score zero, or the engine would never rank anything down."""
        result = score_match(make_input(job_location="Delhi"), now=_NOW)
        assert result.component_scores["location_fit"] == 0.0

    def test_when_nothing_at_all_is_knowable_the_score_is_zero_with_a_full_unknown_list(self):
        """The caller pairs this with the unknown list to render "not enough information"
        rather than a 0% match badge."""
        result = score_match(
            make_input(
                student_role_family=None,
                student_skills=[],
                student_experience_level=None,
                preferred_location=None,
                job_skills=[],
                job_posted_at=None,
            ),
            now=_NOW,
        )
        assert result.component_scores == {}
        assert result.total_score == 0.0
        assert len(result.unknown_factors) >= 4


class TestComponents:
    def test_skill_overlap_is_the_fraction_of_the_jobs_skills_the_student_has(self):
        result = score_match(
            make_input(student_skills=["python"], job_skills=["python", "sql", "aws", "docker"]),
            now=_NOW,
        )
        assert result.component_scores["skill_overlap"] == 0.25
        assert result.matched_skills == ["python"]
        assert result.missing_skills == ["sql", "aws", "docker"]

    def test_remote_jobs_satisfy_a_location_preference_regardless_of_city(self):
        result = score_match(
            make_input(preferred_location="Bengaluru", job_location="Delhi", job_work_mode="remote"),
            now=_NOW,
        )
        assert result.component_scores["location_fit"] == 1.0

    def test_wanting_remote_and_getting_onsite_scores_zero(self):
        result = score_match(
            make_input(preferred_work_mode="remote", job_work_mode="onsite"), now=_NOW
        )
        assert result.component_scores["location_fit"] == 0.0

    def test_wanting_remote_when_the_advert_is_silent_is_unknown_not_a_rejection(self):
        result = score_match(
            make_input(preferred_work_mode="remote", job_work_mode=None), now=_NOW
        )
        assert "location_fit" not in result.component_scores
        assert any("remote" in f for f in result.unknown_factors)

    def test_experience_fit_decays_with_distance_up_the_ladder(self):
        near = score_match(make_input(job_seniority="junior"), now=_NOW)
        far = score_match(make_input(job_seniority="senior"), now=_NOW)
        assert near.component_scores["experience_fit"] > far.component_scores["experience_fit"]

    def test_an_internship_is_treated_as_a_fresher_role(self):
        result = score_match(
            make_input(student_experience_level="fresher", job_seniority="intern"), now=_NOW
        )
        assert result.component_scores["experience_fit"] == 1.0

    def test_freshness_decays_and_never_goes_negative(self):
        fresh = score_match(make_input(job_posted_at=_NOW), now=_NOW)
        old = score_match(make_input(job_posted_at=_NOW - datetime.timedelta(days=15)), now=_NOW)
        ancient = score_match(make_input(job_posted_at=_NOW - datetime.timedelta(days=900)), now=_NOW)
        assert fresh.component_scores["freshness"] == 1.0
        assert 0.0 < old.component_scores["freshness"] < 1.0
        assert ancient.component_scores["freshness"] == 0.0

    def test_a_future_posted_date_does_not_produce_a_score_above_one(self):
        """Provider clock skew is real; a negative age must clamp rather than inflate."""
        result = score_match(make_input(job_posted_at=_NOW + datetime.timedelta(days=5)), now=_NOW)
        assert result.component_scores["freshness"] == 1.0

    def test_no_employment_type_preference_means_the_factor_is_simply_not_scored(self):
        """Distinct from an unknown: the student said "anything is fine", so this is not a gap
        to report to them."""
        result = score_match(make_input(preferred_employment_type=None), now=_NOW)
        assert "employment_type_fit" not in result.component_scores
        assert not any("employment type" in f for f in result.unknown_factors)

    def test_weights_are_declared_for_every_component_the_engine_can_emit(self):
        """Guards the renormalization: a component with no weight entry would raise a KeyError
        inside the weighted mean."""
        emitted = set(
            score_match(
                make_input(preferred_employment_type="full_time", job_employment_type="full_time"),
                now=_NOW,
            ).component_scores
        )
        assert emitted <= set(WEIGHTS)


class TestApplyReadiness:
    def test_high_coverage_is_ready_and_low_coverage_is_a_gap(self):
        ready = score_match(
            make_input(student_skills=["python", "sql"], job_skills=["python", "sql"]), now=_NOW
        )
        gap = score_match(
            make_input(student_skills=["python"], job_skills=["aws", "docker", "kubernetes"]),
            now=_NOW,
        )
        assert ready.apply_readiness == "ready"
        assert gap.apply_readiness == "gap"

    def test_readiness_is_none_when_there_is_no_evidence_to_judge_it_on(self):
        """Spec section 5 requires this be based only on real evidence. A verdict computed from
        nothing would be exactly the fabricated confidence signal the spec forbids."""
        assert score_match(make_input(job_skills=[]), now=_NOW).apply_readiness is None
        assert score_match(make_input(student_skills=[]), now=_NOW).apply_readiness is None


class TestExplanation:
    def test_reasons_cite_the_same_facts_the_components_were_computed_from(self):
        result = score_match(make_input(), now=_NOW)
        joined = " ".join(result.reasons)
        assert "software engineer" in joined.lower()
        assert "2 of 2 skills" in joined

    def test_a_rationale_is_always_present_even_with_nothing_knowable(self):
        result = score_match(
            make_input(
                student_role_family=None,
                student_skills=[],
                student_experience_level=None,
                preferred_location=None,
                job_skills=[],
                job_posted_at=None,
            ),
            now=_NOW,
        )
        assert "Not enough information" in result.rationale

    def test_missing_skills_are_named_so_the_gap_is_actionable(self):
        result = score_match(
            make_input(student_skills=["python"], job_skills=["python", "kubernetes"]), now=_NOW
        )
        assert "kubernetes" in " ".join(result.reasons)


class TestRankingLabel:
    """Spec phase 6's four honest labels. Each describes measured fit, never an outcome."""

    def test_the_top_ranked_strong_result_is_the_best_match(self):
        assert (
            matching.ranking_label(
                rank=0, total_score=0.82, component_count=4, apply_readiness="ready"
            )
            == "best_match"
        )

    def test_an_equally_strong_result_further_down_is_a_strong_fit_not_a_second_best_match(self):
        assert (
            matching.ranking_label(
                rank=1, total_score=0.82, component_count=4, apply_readiness="ready"
            )
            == "strong_fit"
        )

    def test_a_job_viewed_on_its_own_can_never_be_the_best_match(self):
        # Opening one saved job must not label it the best of a set that was never ranked.
        assert (
            matching.ranking_label(
                rank=None, total_score=0.95, component_count=5, apply_readiness="ready"
            )
            == "strong_fit"
        )

    def test_a_middling_score_is_a_stretch(self):
        assert (
            matching.ranking_label(
                rank=2, total_score=0.45, component_count=3, apply_readiness="stretch"
            )
            == "stretch_opportunity"
        )

    def test_a_large_skill_gap_is_framed_as_something_to_build_towards(self):
        assert (
            matching.ranking_label(
                rank=3, total_score=0.5, component_count=3, apply_readiness="gap"
            )
            == "skill_build_opportunity"
        )

    def test_nothing_evaluable_produces_no_label_rather_than_the_worst_one(self):
        # With no component evaluated the total is 0.0 by construction, and calling that a
        # "Skill-Build Opportunity" would dress an absence of information up as a verdict.
        assert (
            matching.ranking_label(
                rank=0, total_score=0.0, component_count=0, apply_readiness=None
            )
            is None
        )

    def test_every_label_produced_is_one_of_the_four_declared(self):
        produced = {
            matching.ranking_label(
                rank=rank, total_score=score, component_count=3, apply_readiness=readiness
            )
            for rank in (0, 1, None)
            for score in (0.0, 0.2, 0.4, 0.7, 1.0)
            for readiness in (None, "ready", "stretch", "gap")
        }
        assert produced <= set(matching.RANKING_LABELS)
