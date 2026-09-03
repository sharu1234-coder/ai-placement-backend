import datetime

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
from app.models.project import Project
from app.models.readiness_snapshot import ReadinessSnapshot
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.services import notification_service
from app.services.career_twin_service import get_current

_UTC = datetime.timezone.utc


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _profile(db, target_role_id=None) -> StudentProfile:
    profile = StudentProfile(user_id=1, target_role_id=target_role_id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def test_no_role_context_returns_has_role_false(db):
    profile = _profile(db)
    twin = get_current(db, profile)
    assert twin.has_role is False
    assert twin.role_source == "none"
    assert twin.readiness_percent is None
    assert "target role" in twin.reasons[0].lower()


def test_target_role_fallback_when_no_career_path(db):
    role = Role(name="SDE", category="Engineering")
    db.add(role)
    db.commit()
    db.refresh(role)
    db.add(RoleSkillWeight(role_id=role.id, skill="DSA", weight=1.0, assessment_type="coding"))
    db.commit()

    profile = _profile(db, target_role_id=role.id)
    twin = get_current(db, profile)

    assert twin.has_role is True
    assert twin.role_source == "target_role"
    assert twin.display_role_name == "SDE"
    assert twin.is_estimate is False
    assert [s.skill for s in twin.skills] == ["DSA"]
    assert twin.skills[0].state == "claimed"  # no evidence at all yet
    assert twin.skills[0].confidence_percent == 0


def test_career_path_takes_precedence_over_target_role(db):
    role = Role(name="Data Analyst", category="Data")
    db.add(role)
    db.commit()
    db.refresh(role)
    db.add(RoleSkillWeight(role_id=role.id, skill="Excel", weight=1.0, assessment_type="tool"))
    db.commit()

    profile = _profile(db, target_role_id=role.id)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()

    twin = get_current(db, profile)
    assert twin.role_source == "career_path"
    assert twin.display_role_name == "Software Engineer"
    assert [s.skill for s in twin.skills] == ["Python"]


def test_ai_generated_career_path_is_flagged_as_estimate(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="custom:prompt-engineer",
            display_role_name="Prompt Engineer",
            is_primary=True,
            generated_via="ai_generated",
            role_overview={"core_skills": ["Prompt Design"], "tools": []},
        )
    )
    db.commit()

    twin = get_current(db, profile)
    assert twin.is_estimate is True


def test_career_path_with_empty_role_overview_has_no_fabricated_skills(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="custom:novel-role",
            display_role_name="Novel Role",
            is_primary=True,
            generated_via="fallback",
            role_overview={},
        )
    )
    db.commit()

    twin = get_current(db, profile)
    assert twin.has_role is True
    assert twin.skills == []
    assert twin.readiness_percent is None
    assert "no required skills" in twin.reasons[0].lower()


def test_readiness_is_weighted_confidence_not_a_raw_score_average(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python", "SQL"], "tools": ["Docker"]},
        )
    )
    db.commit()

    blueprint = AssessmentBlueprint(role_id=None, jd_id=None, profile_id=profile.id, source="x", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile.id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    # Python: single assessment source -> "supported" (confidence 0.45), regardless of the
    # raw 95% score -- confidence, not the raw score, drives readiness.
    db.add(
        AssessmentResult(
            assessment_id=assessment.id, skill_scores={"Python": 0.95}, overall_score=0.95, priority_weaknesses=[]
        )
    )
    db.commit()

    twin = get_current(db, profile)
    python_item = next(s for s in twin.skills if s.skill == "Python")
    assert python_item.measured_score == 95.0
    assert python_item.confidence_percent == 45  # supported, not verified, despite the high raw score
    # readiness = weighted average of confidence (Python 1.0*45 + SQL 1.0*0 + Docker 0.6*0) / 2.6
    assert twin.readiness_percent == round(100 * (1.0 * 0.45) / 2.6)
    assert twin.readiness_percent != 95  # explicitly NOT a naive score average


def test_biggest_gap_prefers_lower_confidence_then_higher_weight(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": ["Docker"]},
        )
    )
    db.commit()
    # Neither skill has any evidence -- both are "claimed" (confidence 0), tied. Python
    # (weight 1.0, core skill) must win the tie over Docker (weight 0.6, a tool).
    twin = get_current(db, profile)
    assert twin.biggest_gap_skill == "Python"
    assert twin.next_action_hint == "Start learning Python fundamentals."


def test_next_action_hint_varies_by_evidence_state(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()
    blueprint = AssessmentBlueprint(role_id=None, jd_id=None, profile_id=profile.id, source="x", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile.id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    old = datetime.datetime.now(_UTC) - datetime.timedelta(days=200)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"Python": 0.9},
            overall_score=0.9,
            priority_weaknesses=[],
            created_at=old,
        )
    )
    db.commit()

    twin = get_current(db, profile)
    assert twin.skills[0].state == "stale"
    assert "refresh" in twin.next_action_hint.lower()


def test_snapshot_is_upserted_not_duplicated_within_the_same_day(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()

    get_current(db, profile)
    get_current(db, profile)

    count = db.query(ReadinessSnapshot).filter(ReadinessSnapshot.profile_id == profile.id).count()
    assert count == 1


def test_readiness_trend_uses_a_real_prior_snapshot(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()

    week_ago = datetime.date.today() - datetime.timedelta(days=7)
    db.add(
        ReadinessSnapshot(
            profile_id=profile.id,
            role_family_key="sde",
            snapshot_date=week_ago,
            readiness_percent=10,
            per_skill_confidence_percent={"Python": 10},
        )
    )
    db.commit()

    twin = get_current(db, profile)
    # No evidence -> today's readiness is 0; trend = 0 - 10 = -10, a real (if unflattering) delta.
    assert twin.readiness_trend_percent == twin.readiness_percent - 10


def _strong_python_evidence(db, profile_id) -> None:
    """Two independent sources (assessment + project) for "Python" -> evidence_engine's
    "strong" state, confidence 0.7 -- see _compute_state_and_confidence."""
    blueprint = AssessmentBlueprint(role_id=None, jd_id=None, profile_id=profile_id, source="x", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(assessment_id=assessment.id, skill_scores={"Python": 0.5}, overall_score=0.5, priority_weaknesses=[])
    )
    db.add(Project(profile_id=profile_id, title="Python ETL Pipeline", description="", tech_stack="Python"))
    db.commit()


def test_crossing_a_readiness_milestone_notifies(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    db.add(
        ReadinessSnapshot(
            profile_id=profile.id,
            role_family_key="sde",
            snapshot_date=yesterday,
            readiness_percent=0,
            per_skill_confidence_percent={"Python": 0},
        )
    )
    db.commit()
    _strong_python_evidence(db, profile.id)

    twin = get_current(db, profile)
    assert twin.readiness_percent == 70  # single-skill role, "strong" confidence 0.7

    pending = notification_service.get_pending(db, profile.id)
    milestone_events = [p for p in pending if p.kind == "readiness_milestone"]
    assert len(milestone_events) == 1
    # Both 50% and 70% were crossed in this one jump -- only the highest is reported.
    assert "70%" in milestone_events[0].title


def test_no_prior_snapshot_never_fabricates_a_milestone_notification(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()
    _strong_python_evidence(db, profile.id)

    # First-ever read: readiness lands at 70% with nothing real to compare against yet.
    get_current(db, profile)

    assert [p for p in notification_service.get_pending(db, profile.id) if p.kind == "readiness_milestone"] == []


def test_crossing_a_milestone_only_notifies_once_even_with_a_same_day_reread(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    db.add(
        ReadinessSnapshot(
            profile_id=profile.id,
            role_family_key="sde",
            snapshot_date=yesterday,
            readiness_percent=0,
            per_skill_confidence_percent={"Python": 0},
        )
    )
    db.commit()
    _strong_python_evidence(db, profile.id)

    get_current(db, profile)  # crosses 50%/70% -- notifies once
    get_current(db, profile)  # same-day reread, unchanged readiness -- must not notify again

    milestone_events = [p for p in notification_service.get_pending(db, profile.id) if p.kind == "readiness_milestone"]
    assert len(milestone_events) == 1


def test_evidence_reaching_strong_notifies_once(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()

    get_current(db, profile)  # first read: Python is "claimed", no evidence yet
    _strong_python_evidence(db, profile.id)
    get_current(db, profile)  # second read: Python just reached "strong"
    get_current(db, profile)  # third read: already strong -- must not re-notify

    pending = notification_service.get_pending(db, profile.id)
    evidence_events = [p for p in pending if p.kind == "evidence_verified"]
    assert len(evidence_events) == 1
    assert "Python" in evidence_events[0].title


def test_readiness_trend_is_none_with_no_prior_snapshot(db):
    profile = _profile(db)
    db.add(
        CareerPath(
            profile_id=profile.id,
            role_family_key="sde",
            display_role_name="Software Engineer",
            is_primary=True,
            generated_via="catalog",
            role_overview={"core_skills": ["Python"], "tools": []},
        )
    )
    db.commit()

    twin = get_current(db, profile)
    assert twin.readiness_trend_percent is None
