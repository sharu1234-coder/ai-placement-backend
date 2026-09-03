"""Today's Mission -- turns Next Best Action's single recommendation into one concrete,
per-day, trackable task. Adds exactly one piece of real mutable state (whether today's
mission has been started/completed) on top of a snapshot of an already-computed,
already-explainable recommendation; it never invents its own ranking logic.

Idempotency is load-bearing here, not incidental -- the spec explicitly calls out duplicate
and concurrent completion as cases that must not corrupt state or error. Two mechanisms make
that true:
  - get_or_create_today: the DB's own unique (profile_id, mission_date) constraint is the
    real guarantee under concurrent creation; this function just turns the resulting
    IntegrityError into a clean re-fetch, exactly like subscription_service.create_order's
    sibling pattern.
  - complete: an atomic conditional UPDATE (WHERE status != 'completed'), not a
    load-mutate-commit -- see subscription_service._activate_paid for the identical shape.
    Whichever caller's UPDATE actually flips the row is irrelevant to the response: both a
    genuine duplicate call and the losing side of a true race get back the one real completed
    row, unchanged on the second call.
"""

import datetime
from typing import Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.assessment import Assessment
from app.models.mission import Mission
from app.models.profile import StudentProfile
from app.services import next_best_action_service, notification_service


class MissionError(Exception):
    pass


class MissionNotFoundError(MissionError):
    pass


class AssessmentNotFoundError(MissionError):
    pass


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def _from_recommendation(profile_id: int, mission_date: datetime.date, recommendation) -> Mission:
    return Mission(
        profile_id=profile_id,
        mission_date=mission_date,
        tier=recommendation.tier,
        title=recommendation.title,
        action_type=recommendation.action_type,
        estimated_minutes=recommendation.estimated_minutes,
        reasons=recommendation.reasons,
        skill=recommendation.skill,
    )


def get_or_create_today(db: Session, profile: StudentProfile) -> Mission:
    today = _today()
    existing = (
        db.query(Mission)
        .populate_existing()
        .filter(Mission.profile_id == profile.id, Mission.mission_date == today)
        .first()
    )
    if existing is not None:
        return existing

    recommendation = next_best_action_service.get_next_best_action(db, profile)
    mission = _from_recommendation(profile.id, today, recommendation)
    db.add(mission)
    try:
        db.commit()
    except IntegrityError:
        # Lost the create race to a concurrent request for the same (profile, day) -- the
        # unique constraint is the real guarantee; fetch and return the winner's row.
        db.rollback()
        return (
            db.query(Mission)
            .filter(Mission.profile_id == profile.id, Mission.mission_date == today)
            .first()
        )
    db.refresh(mission)
    return mission


def _get_owned(db: Session, profile_id: int, mission_id: int) -> Mission:
    mission = db.get(Mission, mission_id)
    if mission is None or mission.profile_id != profile_id:
        raise MissionNotFoundError("Mission not found")
    return mission


def start(db: Session, profile: StudentProfile, mission_id: int) -> Mission:
    mission = _get_owned(db, profile.id, mission_id)
    db.execute(
        update(Mission)
        .where(Mission.id == mission.id, Mission.status == "not_started")
        .values(status="in_progress", started_at=datetime.datetime.now(datetime.timezone.utc))
    )
    db.commit()
    db.refresh(mission)
    return mission


def complete(db: Session, profile: StudentProfile, mission_id: int, assessment_id: Optional[int] = None) -> Mission:
    mission = _get_owned(db, profile.id, mission_id)

    if assessment_id is not None:
        assessment = db.get(Assessment, assessment_id)
        if assessment is None or assessment.profile_id != profile.id:
            raise AssessmentNotFoundError("Assessment not found")

    result = db.execute(
        update(Mission)
        .where(Mission.id == mission.id, Mission.status != "completed")
        .values(
            status="completed",
            completed_at=datetime.datetime.now(datetime.timezone.utc),
            assessment_id=assessment_id,
        )
    )
    # Only the call that actually flipped the row (rowcount == 1) fires a notification -- a
    # genuine duplicate/losing-race call to an already-completed mission must never re-notify.
    if result.rowcount:
        notification_service.enqueue(
            db,
            profile.id,
            kind="mission_completed",
            title="Mission Complete!",
            body=f"You completed today's mission: {mission.title}.",
        )
    db.commit()
    db.refresh(mission)
    return mission
