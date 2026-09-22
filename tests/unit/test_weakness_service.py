import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.skill_gap import SkillGap
from app.services.weakness_service import compute_weaknesses


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_no_data_returns_empty(db):
    assert compute_weaknesses(db, profile_id=1, jd_id=None) == []


def test_jd_skill_gaps_included_when_jd_scoped(db):
    jd = JobDescription(profile_id=1, raw_text="text", source="paste")
    db.add(jd)
    db.commit()
    db.refresh(jd)
    match = JobMatch(jd_id=jd.id, profile_id=1, matching_skills=[], missing_skills=["SQL"], match_score=0.0)
    db.add(match)
    db.commit()
    db.refresh(match)
    db.add(SkillGap(job_match_id=match.id, skill_name="SQL", gap_severity="high", source="jd_match"))
    db.commit()

    weaknesses = compute_weaknesses(db, profile_id=1, jd_id=jd.id)
    assert len(weaknesses) == 1
    assert weaknesses[0].skill_tag == "SQL"
    assert weaknesses[0].severity == "high"
    assert weaknesses[0].sources == ["jd_gap"]


def test_jd_skill_gaps_excluded_when_role_based_scope(db):
    # jd_id=None means role-based -- a JD's gaps must not leak into a role-only roadmap.
    jd = JobDescription(profile_id=1, raw_text="text", source="paste")
    db.add(jd)
    db.commit()
    db.refresh(jd)
    match = JobMatch(jd_id=jd.id, profile_id=1, matching_skills=[], missing_skills=["SQL"], match_score=0.0)
    db.add(match)
    db.commit()
    db.refresh(match)
    db.add(SkillGap(job_match_id=match.id, skill_name="SQL", gap_severity="high", source="jd_match"))
    db.commit()

    assert compute_weaknesses(db, profile_id=1, jd_id=None) == []


def test_assessment_priority_weaknesses_included(db):
    blueprint = AssessmentBlueprint(role_id=1, jd_id=None, profile_id=1, source="default_taxonomy", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=1, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"DSA": 0.3, "OOP": 0.8},
            overall_score=0.55,
            priority_weaknesses=["DSA"],
        )
    )
    db.commit()

    weaknesses = compute_weaknesses(db, profile_id=1, jd_id=None)
    assert len(weaknesses) == 1
    assert weaknesses[0].skill_tag == "DSA"
    assert weaknesses[0].severity == "high"  # 0.3 < 0.4 threshold
    assert weaknesses[0].sources == ["test_result"]


def test_interview_weak_dimensions_included(db):
    interview = Interview(profile_id=1, type="technical", mode="text", jd_id=None, status="completed")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    db.add(
        InterviewEvaluation(
            interview_id=interview.id,
            technical_correctness=0.3,
            relevance=0.9,
            completeness=0.9,
            reasoning=0.9,
            structure=0.9,
            clarity=0.5,
            grammar=0.9,
            speaking_pace=None,
            filler_word_rate=None,
            role_jd_coverage=0.9,
            overall_score=0.7,
            feedback_text="ok",
        )
    )
    db.commit()

    weaknesses = {w.skill_tag: w for w in compute_weaknesses(db, profile_id=1, jd_id=None)}
    assert "interview:technical_correctness" in weaknesses
    assert weaknesses["interview:technical_correctness"].severity == "high"
    assert "interview:clarity" in weaknesses
    assert weaknesses["interview:clarity"].severity == "medium"
    assert "interview:relevance" not in weaknesses  # 0.9 is above the weakness threshold


def test_merges_same_skill_across_sources_and_escalates_severity(db):
    jd = JobDescription(profile_id=1, raw_text="text", source="paste")
    db.add(jd)
    db.commit()
    db.refresh(jd)
    match = JobMatch(jd_id=jd.id, profile_id=1, matching_skills=[], missing_skills=["SQL"], match_score=0.0)
    db.add(match)
    db.commit()
    db.refresh(match)
    db.add(SkillGap(job_match_id=match.id, skill_name="SQL", gap_severity="medium", source="jd_match"))

    blueprint = AssessmentBlueprint(role_id=None, jd_id=jd.id, profile_id=1, source="jd_override", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=1, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id, skill_scores={"SQL": 0.2}, overall_score=0.2, priority_weaknesses=["SQL"]
        )
    )
    db.commit()

    weaknesses = compute_weaknesses(db, profile_id=1, jd_id=jd.id)
    assert len(weaknesses) == 1
    sql_weakness = weaknesses[0]
    assert sql_weakness.severity == "high"  # escalated from medium (jd_gap) by high (test_result)
    assert set(sql_weakness.sources) == {"jd_gap", "test_result"}


def test_sorted_high_severity_first(db):
    blueprint = AssessmentBlueprint(role_id=1, jd_id=None, profile_id=1, source="default_taxonomy", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=1, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"Zebra": 0.5, "Aardvark": 0.1},
            overall_score=0.3,
            priority_weaknesses=["Zebra", "Aardvark"],
        )
    )
    db.commit()

    weaknesses = compute_weaknesses(db, profile_id=1, jd_id=None)
    assert [w.skill_tag for w in weaknesses] == ["Aardvark", "Zebra"]


def test_a_priority_weakness_with_no_recorded_score_is_not_given_an_invented_one(db):
    """Real audit gap, now fixed: a skill flagged as a priority weakness but missing from
    skill_scores used to be substituted with a hard-coded 0.5, which was then compared against
    the 0.4 severity threshold to produce "medium". That number was invented -- it existed
    only to be measured against, and it made an unmeasured weakness indistinguishable from a
    genuinely mid-scoring one. The weakness must still surface (the grader did flag it), but
    at the non-escalating rank, and without pretending a score was recorded."""
    blueprint = AssessmentBlueprint(role_id=1, jd_id=None, profile_id=1, source="default_taxonomy", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=1, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            # "Kafka" is called out as a priority weakness but never scored.
            skill_scores={"DSA": 0.9},
            overall_score=0.9,
            priority_weaknesses=["Kafka"],
        )
    )
    db.commit()

    weaknesses = compute_weaknesses(db, profile_id=1, jd_id=None)
    assert len(weaknesses) == 1
    assert weaknesses[0].skill_tag == "Kafka"
    assert weaknesses[0].severity == "medium"


def test_an_unmeasured_weakness_never_outranks_a_measured_high_severity_one(db):
    """The point of the fix above: sorting puts genuinely-measured severe gaps first. If an
    unscored weakness could land on "high" it would compete for the student's attention with
    a skill actually measured at 0.2, on the strength of a number nobody recorded."""
    blueprint = AssessmentBlueprint(role_id=1, jd_id=None, profile_id=1, source="default_taxonomy", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=1, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"DSA": 0.2},
            overall_score=0.2,
            priority_weaknesses=["DSA", "Kafka"],
        )
    )
    db.commit()

    weaknesses = compute_weaknesses(db, profile_id=1, jd_id=None)
    assert [w.skill_tag for w in weaknesses] == ["DSA", "Kafka"]
    assert weaknesses[0].severity == "high"
    assert weaknesses[1].severity == "medium"


# ==========================================================================================
# Phase 2C's nullable scores, reaching this module
# ==========================================================================================


def test_an_ungraded_interview_evaluation_is_skipped_rather_than_crashing(db):
    """A live regression, found while wiring Career Goals to this engine.

    Phase 2C made an evaluation whose AI review failed a real, storable state: the row exists
    and every score is NULL. This module compares every dimension with `score >= threshold`, so
    a single ungraded interview raised

        TypeError: '>=' not supported between instances of 'NoneType' and 'float'

    out of compute_weaknesses -- and out of every one of its callers: GET /weaknesses, Next Best
    Action, and Today's Mission. One failed AI review broke all three for that student until the
    row was regraded.

    An ungraded evaluation is not a weak result, it is the absence of a result, so it teaches
    this engine nothing -- the same rule, and the same SQL-level exclusion, that
    evidence_engine._fetch_profile_sources already applies.
    """
    interview = Interview(profile_id=1, type="technical", mode="text", status="completed")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    # Exactly what interview_service._persist_ungraded writes: feedback, and no scores at all.
    db.add(InterviewEvaluation(interview_id=interview.id, feedback_text="could not be reviewed"))
    db.commit()

    assert compute_weaknesses(db, profile_id=1, jd_id=None) == []


def test_a_graded_interview_is_still_read_alongside_an_ungraded_one(db):
    """The exclusion must be exactly one row wide: skipping the ungraded evaluation must not
    also lose the real signal sitting next to it."""
    ungraded = Interview(profile_id=1, type="technical", mode="text", status="completed")
    graded = Interview(profile_id=1, type="technical", mode="text", status="completed")
    db.add_all([ungraded, graded])
    db.commit()
    db.refresh(ungraded)
    db.refresh(graded)
    db.add(InterviewEvaluation(interview_id=ungraded.id, feedback_text="could not be reviewed"))
    db.add(
        InterviewEvaluation(
            interview_id=graded.id,
            technical_correctness=0.3,
            relevance=0.9,
            completeness=0.9,
            reasoning=0.9,
            structure=0.9,
            clarity=0.9,
            grammar=0.9,
            role_jd_coverage=0.9,
            overall_score=0.8,
            feedback_text="ok",
        )
    )
    db.commit()

    weaknesses = compute_weaknesses(db, profile_id=1, jd_id=None)

    assert [w.skill_tag for w in weaknesses] == ["interview:technical_correctness"]


def test_a_mode_specific_null_dimension_is_absent_not_weak(db):
    """grammar is text-only; speaking_pace and filler_word_rate are voice-only. A NULL there
    means the dimension does not apply to this interview, which is not the same as scoring
    badly on it -- and must never be reported as a weakness."""
    interview = Interview(profile_id=1, type="technical", mode="voice", status="completed")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    db.add(
        InterviewEvaluation(
            interview_id=interview.id,
            technical_correctness=0.9,
            relevance=0.9,
            completeness=0.9,
            reasoning=0.9,
            structure=0.9,
            clarity=0.9,
            grammar=None,  # voice interview: not applicable
            speaking_pace=None,
            filler_word_rate=None,
            role_jd_coverage=0.9,
            overall_score=0.9,
            feedback_text="ok",
        )
    )
    db.commit()

    assert compute_weaknesses(db, profile_id=1, jd_id=None) == []
