import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.schemas.career_decision import CareerDecisionRoleRead, CareerDecisionSimulationRead
from app.services import career_decision_service as svc


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _profile(db) -> StudentProfile:
    profile = StudentProfile(user_id=1)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _role(db, name: str, skills: dict[str, float]) -> Role:
    role = Role(name=name, category="Engineering")
    db.add(role)
    db.commit()
    db.refresh(role)
    for skill, weight in skills.items():
        db.add(RoleSkillWeight(role_id=role.id, skill=skill, weight=weight, assessment_type="technical"))
    db.commit()
    return role


def _give_assessment_evidence(db, profile, skill_scores: dict[str, float]) -> None:
    blueprint = AssessmentBlueprint(role_id=None, jd_id=None, profile_id=profile.id, source="x", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile.id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(assessment_id=assessment.id, skill_scores=skill_scores, overall_score=0.9, priority_weaknesses=[])
    )
    db.commit()


def test_role_with_no_weights_is_stretch_with_a_clear_reason(db):
    profile = _profile(db)
    role_a = _role(db, "Data Analyst", {"Excel": 1.0})
    role_b = Role(name="Undefined Role", category="Other")
    db.add(role_b)
    db.commit()
    db.refresh(role_b)

    result = svc.simulate(db, profile.id, [role_a.id, role_b.id], None)
    empty = next(r for r in result.roles if r.role_id == role_b.id)
    assert empty.classification == "stretch"
    assert empty.readiness_percent == 0
    assert "no required skills" in empty.reasons[0].lower()


def test_insufficient_profile_data_yields_stretch_for_every_role(db):
    """No evidence at all anywhere -- every candidate role should honestly read as a stretch,
    never a fabricated mid-range number."""
    profile = _profile(db)
    role_a = _role(db, "SDE", {"DSA": 1.0, "OOP": 0.7})
    role_b = _role(db, "Data Analyst", {"SQL": 1.0, "Excel": 0.6})

    result = svc.simulate(db, profile.id, [role_a.id, role_b.id], None)
    assert all(r.classification == "stretch" for r in result.roles)
    assert all(r.readiness_percent == 0 for r in result.roles)


def test_strong_profile_is_classified_best_fit(db):
    profile = _profile(db)
    role = _role(db, "SDE", {"DSA": 1.0})
    other_role = _role(db, "Data Analyst", {"SQL": 1.0})
    # Two independent sources for DSA (assessment + resume) reach evidence_engine's "strong"
    # state (confidence 0.7) -- at weight 1.0 that's exactly a 70% readiness, crossing the
    # best_fit threshold on real, multi-source evidence rather than one lucky score.
    _give_assessment_evidence(db, profile, {"DSA": 0.9})
    resume = Resume(profile_id=profile.id, file_path="x.pdf", raw_text="DSA", parse_status="completed")
    db.add(resume)
    db.commit()
    db.refresh(resume)
    db.add(
        ResumeAnalysis(
            resume_id=resume.id, extracted_skills=["DSA"], extracted_education=[], extracted_experience=[],
            extracted_projects=[], extracted_certifications=[], ats_score=80.0, weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    db.commit()

    result = svc.simulate(db, profile.id, [role.id, other_role.id], None)
    sde_result = next(r for r in result.roles if r.role_id == role.id)
    assert sde_result.readiness_percent >= 70
    assert sde_result.classification == "best_fit"


def test_results_are_ranked_most_ready_first(db):
    profile = _profile(db)
    strong_role = _role(db, "SDE", {"DSA": 1.0})
    weak_role = _role(db, "Data Analyst", {"SQL": 1.0})
    _give_assessment_evidence(db, profile, {"DSA": 0.9})

    result = svc.simulate(db, profile.id, [weak_role.id, strong_role.id], None)
    assert result.roles[0].role_id == strong_role.id
    assert result.roles[0].readiness_percent >= result.roles[1].readiness_percent


def test_shared_skills_are_detected_across_candidate_roles(db):
    profile = _profile(db)
    role_a = _role(db, "Backend Engineer", {"SQL": 1.0, "Python": 1.0})
    role_b = _role(db, "Data Engineer", {"SQL": 1.0, "Spark": 0.8})

    result = svc.simulate(db, profile.id, [role_a.id, role_b.id], None)
    assert result.shared_skills == ["SQL"]


def test_unrealistic_high_weekly_hours_are_clamped(db):
    profile = _profile(db)
    role_a = _role(db, "SDE", {"DSA": 1.0})
    role_b = _role(db, "Data Analyst", {"SQL": 1.0})

    result = svc.simulate(db, profile.id, [role_a.id, role_b.id], 5000)
    assert result.weekly_hours_used == svc._MAX_WEEKLY_HOURS


def test_unrealistic_low_or_negative_weekly_hours_are_clamped(db):
    profile = _profile(db)
    role_a = _role(db, "SDE", {"DSA": 1.0})
    role_b = _role(db, "Data Analyst", {"SQL": 1.0})

    result = svc.simulate(db, profile.id, [role_a.id, role_b.id], -10)
    assert result.weekly_hours_used == svc._MIN_WEEKLY_HOURS


def test_estimated_weeks_only_present_when_weekly_hours_given(db):
    profile = _profile(db)
    role_a = _role(db, "SDE", {"DSA": 1.0})
    role_b = _role(db, "Data Analyst", {"SQL": 1.0})

    without_hours = svc.simulate(db, profile.id, [role_a.id, role_b.id], None)
    assert all(r.estimated_weeks is None for r in without_hours.roles)

    with_hours = svc.simulate(db, profile.id, [role_a.id, role_b.id], 10)
    assert all(r.estimated_weeks is not None for r in with_hours.roles)


def test_unknown_role_id_raises(db):
    profile = _profile(db)
    role_a = _role(db, "SDE", {"DSA": 1.0})

    with pytest.raises(svc.InvalidRoleError):
        svc.simulate(db, profile.id, [role_a.id, 999999], None)


def test_response_schema_has_no_probability_of_employment_field():
    """Enforces the plan's explicit red line: nothing resembling a probability-of-employment
    number should exist anywhere in this schema, so there is nothing to accidentally leak."""
    role_fields = set(CareerDecisionRoleRead.model_fields.keys())
    top_fields = set(CareerDecisionSimulationRead.model_fields.keys())
    for banned in ("probability", "chance", "likelihood", "odds"):
        assert not any(banned in field.lower() for field in role_fields)
        assert not any(banned in field.lower() for field in top_fields)


def test_hours_estimate_is_always_flagged_as_an_estimate(db):
    profile = _profile(db)
    role_a = _role(db, "SDE", {"DSA": 1.0})
    role_b = _role(db, "Data Analyst", {"SQL": 1.0})

    result = svc.simulate(db, profile.id, [role_a.id, role_b.id], None)
    assert all(r.is_estimate for r in result.roles)
    assert len(result.assumptions) > 0
