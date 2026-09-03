import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.profile import StudentProfile
from app.services.skill_passport_service import SkillNotFoundError, get_skill, list_skills


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


def _career_path(db, profile_id, core_skills, tools=None):
    path = CareerPath(
        profile_id=profile_id,
        role_family_key="sde",
        display_role_name="Software Engineer",
        is_primary=True,
        generated_via="catalog",
        role_overview={"core_skills": core_skills, "tools": tools or []},
    )
    db.add(path)
    db.commit()
    return path


def test_list_skills_returns_empty_with_no_role(db):
    profile = _profile(db)
    assert list_skills(db, profile) == []


def test_list_skills_returns_empty_when_role_has_no_skills(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=[])
    assert list_skills(db, profile) == []


def test_unclaimed_skill_has_no_level_and_zero_confidence(db):
    # The product's explicit rule: a user claim (or here, simply nothing at all) must never
    # by itself produce a level/verified status. With zero real evidence, level is omitted
    # entirely rather than defaulted to "Beginner".
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])

    items = list_skills(db, profile)
    assert len(items) == 1
    assert items[0].skill == "Python"
    assert items[0].state == "claimed"
    assert items[0].level is None
    assert items[0].confidence_percent == 0
    assert items[0].evidence == []
    assert "start learning" in items[0].next_action_hint.lower()


def test_skill_with_real_evidence_gets_a_derived_level_and_evidence_trail(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])

    blueprint = AssessmentBlueprint(role_id=None, jd_id=None, profile_id=profile.id, source="x", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile.id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(assessment_id=assessment.id, skill_scores={"Python": 0.9}, overall_score=0.9, priority_weaknesses=[])
    )
    db.commit()

    items = list_skills(db, profile)
    python_item = items[0]
    assert python_item.state == "supported"  # single source
    assert python_item.level == "Beginner"  # _LEVEL_BY_STATE["supported"]
    assert len(python_item.evidence) == 1
    assert python_item.evidence[0].source == "assessment"
    assert python_item.evidence[0].score == 90.0


def test_get_skill_returns_full_detail(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["SQL"])

    item = get_skill(db, profile, "SQL")
    assert item.skill == "SQL"
    assert item.state == "claimed"


def test_get_skill_not_in_current_role_raises_not_found(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["SQL"])

    with pytest.raises(SkillNotFoundError):
        get_skill(db, profile, "Kafka")


def test_get_skill_with_no_role_at_all_raises_not_found(db):
    profile = _profile(db)
    with pytest.raises(SkillNotFoundError):
        get_skill(db, profile, "Python")
