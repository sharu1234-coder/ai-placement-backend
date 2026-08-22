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
