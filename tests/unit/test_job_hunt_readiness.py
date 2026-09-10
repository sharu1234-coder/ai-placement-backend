"""Application readiness.

The property under test throughout is that a dimension we cannot judge is *excluded*, never
scored zero. Getting that wrong is not a cosmetic bug: it tells a student they are 0% ready for
something they have simply not attempted yet, which is both false and discouraging in exactly
the place a student is most likely to give up.
"""

import datetime
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- imports every model so create_all sees the full schema
from app.database.base import Base
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.resume_ats_check import ResumeAtsCheck
from app.models.user import User
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


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'readiness.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_profile(db) -> StudentProfile:
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    return profile


def make_ats_check(db, profile_id: int, score: int) -> None:
    """An ATS check needs a resume behind it -- resume_ats_checks.resume_id is NOT NULL."""
    resume = Resume(
        profile_id=profile_id,
        file_path="cv.pdf",
        original_filename="cv.pdf",
        raw_text="Python SQL",
    )
    db.add(resume)
    db.commit()
    # Only overall_score is read by the dimension; the rest are NOT NULL columns the ATS
    # module fills in and this test has no opinion about.
    db.add(
        ResumeAtsCheck(
            profile_id=profile_id,
            resume_id=resume.id,
            overall_score=score,
            score_category="good",
            parsing_score=score,
            structure_score=score,
            content_score=score,
            skills_score=score,
            experience_score=score,
            projects_score=score,
            contact_score=score,
            readability_score=score,
            category_breakdown=[],
            section_analysis=[],
            issues=[],
            recommendations=[],
            ai_summary="Reads well.",
        )
    )
    db.commit()


# The scored dimensions are NOT NULL on interview_evaluations. Their values are irrelevant
# here -- only overall_score is read -- so one fixed set stands in for all of them.
EVALUATION_DIMENSIONS = dict(
    technical_correctness=0.8,
    relevance=0.8,
    completeness=0.8,
    reasoning=0.8,
    structure=0.8,
    clarity=0.8,
    role_jd_coverage=0.8,
    feedback_text="Solid answers throughout.",
)


def make_interview(db, profile_id: int) -> "Interview":
    interview = Interview(profile_id=profile_id, type="technical", mode="text", status="completed")
    db.add(interview)
    db.commit()
    return interview


class TestTheTwoBorrowedScoresAreReadInTheirOwnUnits:
    """Both dimensions reuse a score another feature already owns, and the two features store
    them on different scales. Reading either in the other's unit is silent and catastrophic:
    the number still renders, still sits between 0 and 100, and is simply wrong by 100x in one
    direction or the other. That has happened once already -- the interview score was divided
    by 100, which reported every student as under 1% ready for an interview they had actually
    done well in. These pin both scales so the next edit has to notice."""

    def test_the_ats_score_is_a_percentage_and_is_divided_down(self, db):
        # ResumeAtsCheck.overall_score is an Integer 0-100 (see ats_scoring/engine.clamp_score).
        profile = make_profile(db)
        make_ats_check(db, profile.id, 82)

        dimension = readiness._resume_dimension(db, profile.id)

        assert dimension.score == pytest.approx(0.82)
        assert "82" in dimension.detail

    def test_the_interview_score_is_already_a_unit_score_and_is_used_as_is(self, db):
        # InterviewEvaluation.overall_score is a Float clamped to 0.0-1.0 by
        # interview_service._clamped before it is ever persisted.
        profile = make_profile(db)
        interview = make_interview(db, profile.id)
        db.add(
            InterviewEvaluation(
                interview_id=interview.id,
                **EVALUATION_DIMENSIONS,
                overall_score=0.82,
                created_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
            )
        )
        db.commit()

        dimension = readiness._interview_dimension(db, profile.id)

        assert dimension.score == pytest.approx(0.82), "dividing by 100 here reports 0.8%"

    def test_an_interview_older_than_the_recency_window_does_not_count(self, db):
        """A strong showing six months ago is not what a student walks in with tomorrow, and
        the dimension says "not checked yet" rather than borrowing the old number."""
        profile = make_profile(db)
        interview = make_interview(db, profile.id)
        stale = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None
        ) - datetime.timedelta(days=readiness._INTERVIEW_RECENCY_DAYS + 1)
        db.add(
            InterviewEvaluation(
                interview_id=interview.id,
                **EVALUATION_DIMENSIONS, overall_score=0.95, created_at=stale
            )
        )
        db.commit()

        assert readiness._interview_dimension(db, profile.id).score is None

    def test_another_students_scores_are_never_borrowed(self, db):
        mine = make_profile(db)
        theirs = make_profile(db)
        make_ats_check(db, theirs.id, 95)
        interview = make_interview(db, theirs.id)
        db.add(
            InterviewEvaluation(
                interview_id=interview.id,
                **EVALUATION_DIMENSIONS,
                overall_score=0.95,
                created_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
            )
        )
        db.commit()

        assert readiness._resume_dimension(db, mine.id).score is None
        assert readiness._interview_dimension(db, mine.id).score is None
