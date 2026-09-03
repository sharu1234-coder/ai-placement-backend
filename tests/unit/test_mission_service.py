import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.career_path import CareerPath
from app.models.mission import Mission
from app.models.profile import StudentProfile
from app.services import mission_service, notification_service


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


_next_user_id = [1]


def _profile(db) -> StudentProfile:
    profile = StudentProfile(user_id=_next_user_id[0])
    _next_user_id[0] += 1
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _career_path(db, profile_id, core_skills) -> CareerPath:
    path = CareerPath(
        profile_id=profile_id,
        role_family_key="sde",
        display_role_name="Software Engineer",
        is_primary=True,
        generated_via="catalog",
        role_overview={"core_skills": core_skills, "tools": []},
    )
    db.add(path)
    db.commit()
    db.refresh(path)
    return path


def _assessment(db, profile_id) -> Assessment:
    blueprint = AssessmentBlueprint(role_id=None, jd_id=None, profile_id=profile_id, source="x", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return assessment


def test_get_or_create_today_creates_from_next_best_action(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])

    mission = mission_service.get_or_create_today(db, profile)

    assert mission.status == "not_started"
    assert mission.tier == "critical_role_gap"
    assert mission.skill == "Python"
    assert mission.mission_date == datetime.datetime.now(datetime.timezone.utc).date()
    assert len(mission.reasons) > 0


def test_get_or_create_today_with_no_role_still_returns_a_mission(db):
    profile = _profile(db)
    mission = mission_service.get_or_create_today(db, profile)
    assert mission.tier is None
    assert mission.action_type == "maintenance"


def test_get_or_create_today_is_idempotent_same_day(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])

    first = mission_service.get_or_create_today(db, profile)
    second = mission_service.get_or_create_today(db, profile)

    assert first.id == second.id


def test_get_or_create_today_concurrent_creation_returns_one_winner(db, engine):
    """Two near-simultaneous requests on separate sessions against the same in-memory DB --
    both see no existing row and attempt to create one; the unique (profile_id, mission_date)
    constraint means only one insert lands, and the loser's IntegrityError path must return
    that same winning row rather than crashing."""
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])

    session_b = sessionmaker(bind=engine)()
    try:
        winner = mission_service.get_or_create_today(session_b, profile)
    finally:
        session_b.close()

    loser_result = mission_service.get_or_create_today(db, profile)

    assert loser_result.id == winner.id
    assert db.query(Mission).filter(Mission.profile_id == profile.id).count() == 1


def test_start_transitions_not_started_to_in_progress(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)

    started = mission_service.start(db, profile, mission.id)

    assert started.status == "in_progress"
    assert started.started_at is not None


def test_start_is_idempotent_and_does_not_reset_started_at(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)

    first = mission_service.start(db, profile, mission.id)
    first_started_at = first.started_at
    second = mission_service.start(db, profile, mission.id)

    assert second.status == "in_progress"
    assert second.started_at == first_started_at


def test_start_on_a_completed_mission_does_not_revert_it(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)
    mission_service.complete(db, profile, mission.id)

    result = mission_service.start(db, profile, mission.id)

    assert result.status == "completed"


def test_start_unowned_mission_raises_not_found(db):
    profile = _profile(db)
    other = _profile(db)
    _career_path(db, other.id, core_skills=["Python"])
    other_mission = mission_service.get_or_create_today(db, other)

    with pytest.raises(mission_service.MissionNotFoundError):
        mission_service.start(db, profile, other_mission.id)


def test_complete_sets_completed_state_and_links_assessment(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)
    assessment = _assessment(db, profile.id)

    completed = mission_service.complete(db, profile, mission.id, assessment_id=assessment.id)

    assert completed.status == "completed"
    assert completed.completed_at is not None
    assert completed.assessment_id == assessment.id


def test_complete_without_assessment_id_still_completes(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)

    completed = mission_service.complete(db, profile, mission.id)

    assert completed.status == "completed"
    assert completed.assessment_id is None


def test_duplicate_completion_does_not_change_completed_at_or_assessment(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)
    assessment = _assessment(db, profile.id)

    first = mission_service.complete(db, profile, mission.id, assessment_id=assessment.id)
    # A duplicate completion call (e.g. a retried request) with no assessment_id must NOT
    # blank out the real one already recorded, and must not error.
    second = mission_service.complete(db, profile, mission.id)

    assert second.completed_at == first.completed_at
    assert second.assessment_id == assessment.id


def test_concurrent_completion_only_one_write_lands_no_error(db, engine):
    """Genuinely simulates two near-simultaneous completion requests via two separate DB
    sessions against the same in-memory database, mirroring
    test_subscription_service's concurrent-verify race test."""
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)
    assessment = _assessment(db, profile.id)

    session_b = sessionmaker(bind=engine)()
    try:
        winner = mission_service.complete(session_b, profile, mission.id, assessment_id=assessment.id)
    finally:
        session_b.close()

    # session `db`'s in-memory copy of `mission` is now stale (still "not_started") -- exactly
    # the state a truly concurrent request would be in.
    result = mission_service.complete(db, profile, mission.id)

    assert result.status == "completed"
    assert result.completed_at == winner.completed_at
    assert result.assessment_id == winner.assessment_id


def test_completion_enqueues_exactly_one_notification(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)

    mission_service.complete(db, profile, mission.id)

    pending = notification_service.get_pending(db, profile.id)
    assert len(pending) == 1
    assert pending[0].kind == "mission_completed"
    assert mission.title in pending[0].body


def test_duplicate_completion_does_not_enqueue_a_second_notification(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)

    mission_service.complete(db, profile, mission.id)
    mission_service.complete(db, profile, mission.id)  # duplicate call -- must not re-notify

    assert len(notification_service.get_pending(db, profile.id)) == 1


def test_concurrent_completion_only_the_winner_enqueues_a_notification(db, engine):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)

    session_b = sessionmaker(bind=engine)()
    try:
        mission_service.complete(session_b, profile, mission.id)
    finally:
        session_b.close()

    mission_service.complete(db, profile, mission.id)  # loses the race -- stale in-memory row

    assert len(notification_service.get_pending(db, profile.id)) == 1


def test_complete_with_unknown_assessment_id_raises(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)

    with pytest.raises(mission_service.AssessmentNotFoundError):
        mission_service.complete(db, profile, mission.id, assessment_id=999)


def test_complete_with_another_profiles_assessment_id_raises(db):
    profile = _profile(db)
    other = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    mission = mission_service.get_or_create_today(db, profile)
    other_assessment = _assessment(db, other.id)

    with pytest.raises(mission_service.AssessmentNotFoundError):
        mission_service.complete(db, profile, mission.id, assessment_id=other_assessment.id)


def test_complete_unowned_mission_raises_not_found(db):
    profile = _profile(db)
    other = _profile(db)
    _career_path(db, other.id, core_skills=["Python"])
    other_mission = mission_service.get_or_create_today(db, other)

    with pytest.raises(mission_service.MissionNotFoundError):
        mission_service.complete(db, profile, other_mission.id)
