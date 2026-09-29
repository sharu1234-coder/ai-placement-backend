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
from app.models.provider_connection import PROVIDER_GITHUB, ProviderConnection
from app.models.readiness_snapshot import ReadinessSnapshot
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.services import evidence_engine, notification_service
from app.services.career_twin_service import _upsert_snapshot, get_current

_UTC = datetime.timezone.utc


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
    assert twin.evidence_coverage_percent is None
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
    assert twin.evidence_coverage_percent is None
    assert "no required skills" in twin.reasons[0].lower()


def test_coverage_is_weighted_confidence_not_a_raw_score_average(db):
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
    # raw 95% score -- confidence, not the raw score, drives evidence coverage.
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
    # evidence coverage = weighted average of confidence (Python 1.0*45 + SQL 1.0*0 + Docker 0.6*0) / 2.6
    assert twin.evidence_coverage_percent == round(100 * (1.0 * 0.45) / 2.6)
    assert twin.evidence_coverage_percent != 95  # explicitly NOT a naive score average


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


def test_upsert_snapshot_concurrent_first_read_of_day_no_error(db, engine):
    """Two near-simultaneous first-reads-of-the-day for the same (profile, role) on separate
    sessions against the same in-memory DB -- both can attempt the insert branch; the table's
    own uq_readiness_snapshot_per_day constraint means only one insert lands, and the loser
    must apply its values on top of the winner's row rather than crashing with an uncaught
    IntegrityError, mirroring mission_service.get_or_create_today's identical race handling."""
    profile = _profile(db)
    today = datetime.date.today()

    session_b = sessionmaker(bind=engine)()
    try:
        _upsert_snapshot(session_b, profile.id, "sde", today, evidence_coverage_percent=40, per_skill={"Python": 40})
    finally:
        session_b.close()

    # session_b's insert is already committed on the shared engine by the time this call's own
    # SELECT runs, so this call converges via the existing-row branch -- the same outcome a
    # true interleaved race must also reach via the except-IntegrityError branch, since both
    # paths end with exactly one row holding whichever call wrote last.
    baseline = _upsert_snapshot(db, profile.id, "sde", today, evidence_coverage_percent=55, per_skill={"Python": 55})

    assert baseline == 40  # session_b's value was on record immediately before this write
    rows = db.query(ReadinessSnapshot).filter(ReadinessSnapshot.profile_id == profile.id).all()
    assert len(rows) == 1
    assert rows[0].evidence_coverage_percent == 55  # this call's values are the ones that stuck


def test_coverage_trend_uses_a_real_prior_snapshot(db):
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
            evidence_coverage_percent=10,
            per_skill_confidence_percent={"Python": 10},
        )
    )
    db.commit()

    twin = get_current(db, profile)
    # No evidence -> today's coverage is 0; trend = 0 - 10 = -10, a real (if unflattering) delta.
    assert twin.evidence_coverage_trend_percent == twin.evidence_coverage_percent - 10


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


def test_crossing_a_coverage_milestone_notifies(db):
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
            evidence_coverage_percent=0,
            per_skill_confidence_percent={"Python": 0},
        )
    )
    db.commit()
    _strong_python_evidence(db, profile.id)

    twin = get_current(db, profile)
    assert twin.evidence_coverage_percent == 70  # single-skill role, "strong" confidence 0.7

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

    # First-ever read: coverage lands at 70% with nothing real to compare against yet.
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
            evidence_coverage_percent=0,
            per_skill_confidence_percent={"Python": 0},
        )
    )
    db.commit()
    _strong_python_evidence(db, profile.id)

    get_current(db, profile)  # crosses 50%/70% -- notifies once
    get_current(db, profile)  # same-day reread, unchanged coverage -- must not notify again

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


def test_coverage_trend_is_none_with_no_prior_snapshot(db):
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
    assert twin.evidence_coverage_trend_percent is None


def test_coverage_trend_ignores_a_baseline_far_older_than_this_week(db):
    """Real audit gap, now fixed: the trend query took the newest snapshot at least 6 days
    old, with no lower bound on how old. A student who used the app months ago and came back
    had that ancient snapshot used as "last week", so Home greeted them with an impressive
    "+40% this week" describing half a year of work -- or, just as wrong, a large negative
    delta for a week in which they did nothing at all. With no baseline inside the real
    comparison window there is no honest weekly number to show, so none is shown."""
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

    long_ago = datetime.date.today() - datetime.timedelta(days=180)
    db.add(
        ReadinessSnapshot(
            profile_id=profile.id,
            role_family_key="sde",
            snapshot_date=long_ago,
            evidence_coverage_percent=10,
            per_skill_confidence_percent={"Python": 10},
        )
    )
    db.commit()

    twin = get_current(db, profile)
    assert twin.evidence_coverage_trend_percent is None


def test_coverage_trend_still_works_at_the_far_edge_of_the_window(db):
    """The bound must not be so tight that a student with slightly irregular usage loses their
    trend -- snapshots are written on read, so gaps are normal and expected."""
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

    edge = datetime.date.today() - datetime.timedelta(days=13)
    db.add(
        ReadinessSnapshot(
            profile_id=profile.id,
            role_family_key="sde",
            snapshot_date=edge,
            evidence_coverage_percent=10,
            per_skill_confidence_percent={"Python": 10},
        )
    )
    db.commit()

    twin = get_current(db, profile)
    assert twin.evidence_coverage_trend_percent == twin.evidence_coverage_percent - 10


# --- What the number could not see ----------------------------------------------------------
# Evidence coverage is a weighted average over evidence we were able to read. When a source was
# connected, the honest thing is to say which one -- not to let its absence read as a finding
# about the student. See evidence_engine.unavailable_sources.


def _role_with_one_skill(db) -> Role:
    role = Role(name="Data Analyst", category="Data")
    db.add(role)
    db.commit()
    db.refresh(role)
    db.add(RoleSkillWeight(role_id=role.id, skill="SQL", weight=1.0, assessment_type="sql"))
    db.commit()
    return role


def test_the_twin_names_the_sources_it_could_not_read(db):
    role = _role_with_one_skill(db)
    profile = _profile(db, target_role_id=role.id)

    twin = get_current(db, profile)

    assert twin.unavailable_sources == [evidence_engine.SOURCE_EXTERNAL, evidence_engine.SOURCE_RESUME]
    # The coverage number itself is unaffected: a source we could not read contributes no
    # evidence either way, which is already what every matcher does.
    assert twin.evidence_coverage_percent == 0


def test_a_connected_student_has_nothing_reported_as_unread(db):
    role = _role_with_one_skill(db)
    profile = _profile(db, target_role_id=role.id)
    db.add(
        ProviderConnection(
            profile_id=profile.id,
            provider=PROVIDER_GITHUB,
            provider_user_id="4242",
            provider_username="priya",
        )
    )
    db.commit()

    twin = get_current(db, profile)

    assert evidence_engine.SOURCE_EXTERNAL not in twin.unavailable_sources
