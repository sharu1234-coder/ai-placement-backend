import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.services.skill_graph import (
    get_role_skill_weights,
    get_role_skill_weights_by_family_key,
    get_role_skill_weights_by_role_id,
    get_role_skill_weights_from_overview,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_role_id_weights_no_rows_returns_empty(db):
    assert get_role_skill_weights_by_role_id(db, role_id=999) == {}


def test_role_id_weights_reads_role_skill_weight_rows(db):
    role = Role(name="SDE", category="Engineering")
    db.add(role)
    db.commit()
    db.refresh(role)
    db.add_all(
        [
            RoleSkillWeight(role_id=role.id, skill="DSA", weight=1.0, assessment_type="coding"),
            RoleSkillWeight(role_id=role.id, skill="Aptitude", weight=0.5, assessment_type="aptitude"),
        ]
    )
    db.commit()

    weights = get_role_skill_weights_by_role_id(db, role.id)
    assert weights == {"DSA": 1.0, "Aptitude": 0.5}


def test_family_key_weights_unregistered_key_returns_empty():
    assert get_role_skill_weights_by_family_key("custom:some-novel-role") == {}


def test_family_key_weights_curated_role_weights_core_skills_above_tools():
    weights = get_role_skill_weights_by_family_key("sde")
    assert weights["Data Structures & Algorithms"] == 1.0
    assert weights["Git/GitHub"] == 0.6
    assert weights["Git/GitHub"] < weights["Data Structures & Algorithms"]


def test_family_key_weights_core_skill_not_overridden_by_tool_default():
    # "Git & Version Control" is a core skill for sde; "Git/GitHub" is a separate tools
    # entry -- setdefault() must never downgrade an existing core-skill weight.
    weights = get_role_skill_weights_by_family_key("sde")
    assert weights["Git & Version Control"] == 1.0


def test_combined_weights_family_key_overrides_role_id_on_overlap(db):
    role = Role(name="SDE", category="Engineering")
    db.add(role)
    db.commit()
    db.refresh(role)
    db.add(RoleSkillWeight(role_id=role.id, skill="Data Structures & Algorithms", weight=0.2, assessment_type="coding"))
    db.commit()

    weights = get_role_skill_weights(db, role_id=role.id, role_family_key="sde")
    # Family-key content (1.0) wins over the older RoleSkillWeight row (0.2) for the same skill.
    assert weights["Data Structures & Algorithms"] == 1.0
    # But a skill only present via the RoleSkillWeight path is still carried through.


def test_combined_weights_role_id_only(db):
    role = Role(name="SDE", category="Engineering")
    db.add(role)
    db.commit()
    db.refresh(role)
    db.add(RoleSkillWeight(role_id=role.id, skill="OS", weight=0.5, assessment_type="technical"))
    db.commit()

    weights = get_role_skill_weights(db, role_id=role.id)
    assert weights == {"OS": 0.5}


def test_combined_weights_neither_given_returns_empty(db):
    assert get_role_skill_weights(db) == {}


def test_weights_from_overview_covers_custom_ai_assisted_roles():
    # An AI-assisted CareerPath's role_family_key ("custom:...") has no entry in the static
    # ROLE_FAMILIES catalog -- get_role_skill_weights_by_family_key would return {} for it.
    # get_role_skill_weights_from_overview must still produce real weights from the CareerPath
    # row's own persisted role_overview snapshot.
    overview = {"core_skills": ["Prompt Engineering"], "tools": ["LangChain"]}
    weights = get_role_skill_weights_from_overview(overview)
    assert weights == {"Prompt Engineering": 1.0, "LangChain": 0.6}


def test_weights_from_overview_missing_keys_returns_empty():
    assert get_role_skill_weights_from_overview({}) == {}


def test_family_key_weights_delegates_to_overview_form():
    assert get_role_skill_weights_by_family_key("sde") == get_role_skill_weights_from_overview(
        {"core_skills": [
            "Data Structures & Algorithms", "Object-Oriented Programming", "Git & Version Control",
            "REST API Design", "Databases (SQL)", "Problem Solving", "Testing & Debugging",
        ], "tools": ["Git/GitHub", "VS Code or IntelliJ", "Postman", "Docker (basics)", "PostgreSQL/MySQL"]}
    )
