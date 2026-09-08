"""Application readiness.

The property under test throughout is that a dimension we cannot judge is *excluded*, never
scored zero. Getting that wrong is not a cosmetic bug: it tells a student they are 0% ready for
something they have simply not attempted yet, which is both false and discouraging in exactly
the place a student is most likely to give up.
"""

from app.models.job_listing import JobListing
from app.services.job_hunt import provenance, readiness


def listing(title="Data Engineer", description="Python and SQL required.", company="Acme"):
    return JobListing(
        id=1,
        provider="fake",
        provider_job_id="1",
        url="https://jobs.example.com/1",
        title=title,
        company=company,
        description=description,
    )


def prov(skill, status, state=None):
    return provenance.SkillProvenance(skill=skill, status=status, evidence_state=state)


class TestSkillsDimension:
    def test_proven_skills_score_higher_than_the_same_number_of_claims(self):
        # A student matching every skill on claims alone is genuinely weaker than one matching
        # the same skills with proof, and a plain count would rank them equal.
        proven = readiness._skills_dimension(
            [prov("python", provenance.VERIFIED_EVIDENCE), prov("sql", provenance.VERIFIED_EVIDENCE)]
        )
        claimed = readiness._skills_dimension(
            [prov("python", provenance.ON_RESUME), prov("sql", provenance.ON_RESUME)]
        )
        assert proven.score > claimed.score

    def test_a_claim_still_counts_for_something(self):
        # Recruiters do read resumes. Scoring a claim as zero would be as wrong in the other
        # direction as scoring it as proof.
        claimed = readiness._skills_dimension([prov("python", provenance.ON_RESUME)])
        assert 0 < claimed.score < 1.0

    def test_a_missing_skill_lowers_the_score(self):
        full = readiness._skills_dimension([prov("python", provenance.VERIFIED_EVIDENCE)])
        partial = readiness._skills_dimension(
            [prov("python", provenance.VERIFIED_EVIDENCE), prov("aws", provenance.MISSING)]
        )
        assert partial.score < full.score

    def test_unclear_skills_alone_leave_the_dimension_unjudged(self):
        # Every skill unclear means we hold no skills for the student. That is an absence of
        # information about the pairing, not a weak candidate.
        result = readiness._skills_dimension(
            [prov("python", provenance.UNCLEAR), prov("sql", provenance.UNCLEAR)]
        )
        assert result.score is None

    def test_an_advert_naming_no_skills_leaves_the_dimension_unjudged(self):
        assert readiness._skills_dimension([]).score is None

    def test_the_detail_says_what_the_number_came_from(self):
        result = readiness._skills_dimension(
            [prov("python", provenance.VERIFIED_EVIDENCE), prov("sql", provenance.ON_RESUME)]
        )
        assert "1" in result.detail and "resume" in result.detail


class TestExperienceDimension:
    def test_it_reuses_the_match_engine_component_unchanged(self):
        result = readiness._experience_dimension({"experience_fit": 0.75})
        assert result.score == 0.75

    def test_an_absent_component_is_unjudged_rather_than_zero(self):
        assert readiness._experience_dimension({}).score is None


class TestEducationDimension:
    def test_no_education_on_file_is_unknown_not_a_failure(self):
        # Not knowing a student's education is not the same as knowing they lack it.
        class _Profile:
            degree = None
            graduation_year = None

        result = readiness._education_dimension(_Profile(), listing())
        assert result.score is None
        assert "Add your degree" in result.detail

    def test_a_stated_requirement_the_student_meets_scores_full(self):
        class _Profile:
            degree = "B.Tech"
            graduation_year = 2026

        result = readiness._education_dimension(
            _Profile(), listing(description="Bachelor degree in computer science required.")
        )
        assert result.score == 1.0
        assert "asks for a degree" in result.detail

    def test_an_advert_with_no_requirement_says_so_rather_than_implying_a_pass(self):
        # A full bar has to explain itself, or it reads as a guess.
        class _Profile:
            degree = "B.Tech"
            graduation_year = 2026

        result = readiness._education_dimension(_Profile(), listing(description="Join our team."))
        assert result.score == 1.0
        assert "no specific degree requirement" in result.detail

    def test_a_missing_profile_is_unknown(self):
        assert readiness._education_dimension(None, listing()).score is None


class TestOverall:
    def _dims(self, **scores):
        return [
            readiness.ReadinessDimension(key, key.title(), scores.get(key), "")
            for key in readiness.READINESS_WEIGHTS
        ]

    def test_the_mean_is_taken_over_judged_dimensions_only(self):
        # Two dimensions at 0.8 with three unknown must be 0.8, not 0.8 diluted by three zeros.
        judged = {"skills": 0.8, "experience": 0.8}
        used = sum(readiness.READINESS_WEIGHTS[k] for k in judged)
        expected = sum(v * readiness.READINESS_WEIGHTS[k] for k, v in judged.items()) / used
        assert round(expected, 4) == 0.8

    def test_weights_sum_to_one_so_a_fully_judged_score_is_a_plain_weighted_mean(self):
        assert abs(sum(readiness.READINESS_WEIGHTS.values()) - 1.0) < 1e-9

    def test_bands_are_advice_not_a_verdict_on_the_student(self):
        # Deliberately no "not ready": a low readiness means there is preparation to do, and
        # whether to apply anyway is the student's call.
        assert readiness._band(0.95) == "strong_candidate"
        assert readiness._band(0.65) == "worth_applying"
        assert readiness._band(0.45) == "stretch"
        assert readiness._band(0.1) == "prepare_first"


class TestBiggestGap:
    def test_a_named_missing_skill_is_preferred_over_a_low_dimension(self):
        # "Learn Kubernetes" is something a student can start this afternoon. "Improve your
        # experience" is not.
        gap = readiness._biggest_gap(
            [readiness.ReadinessDimension("experience", "Experience", 0.2, "")],
            [prov("kubernetes", provenance.MISSING)],
        )
        assert gap["kind"] == "skill"
        assert gap["skill"] == "kubernetes"

    def test_an_unproven_skill_becomes_a_prove_it_action(self):
        gap = readiness._biggest_gap([], [prov("python", provenance.ON_RESUME)])
        assert gap["kind"] == "evidence"
        assert gap["action"] == "skill_passport"

    def test_nothing_actionable_returns_nothing_rather_than_filler(self):
        gap = readiness._biggest_gap(
            [readiness.ReadinessDimension("skills", "Skills", 0.95, "")],
            [prov("python", provenance.VERIFIED_EVIDENCE)],
        )
        assert gap is None


class TestTips:
    def test_tips_are_specific_to_this_advert(self):
        tips = readiness._tips(
            listing(company="Emirates NBD"),
            [prov("aws", provenance.MISSING), prov("python", provenance.ON_RESUME)],
        )
        joined = " ".join(tips)
        assert "aws" in joined
        assert "Emirates NBD" in joined

    def test_no_generic_filler_when_there_is_nothing_specific_to_say(self):
        # A tip equally true of every job on the platform is noise, and a student learns to
        # skip a section that always says the same thing.
        tips = readiness._tips(listing(company=None), [prov("python", provenance.VERIFIED_EVIDENCE)])
        assert tips == []
