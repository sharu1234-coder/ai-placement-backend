"""Placement Arena -- a timed-mode + personal-best wrapper around the existing Assessment
engine (assessment_run_service), not a new question bank. Starting a challenge attempt
creates a single-section AssessmentBlueprint + Assessment exactly like Prepare Me does, so
question generation/grading/scoring is the exact same trusted, already-tested code path --
the client drives it via the existing `/assessments/{id}/next-question` and
`/assessments/{id}/answer` endpoints unchanged. Arena adds only: a time limit (from the
static challenges.py catalog), an ArenaAttempt row linking a challenge_key to that Assessment,
and a personal-best diff computed from this profile's own real prior ArenaAttempt rows for
the same challenge_key -- never a global/anonymous comparison (deferred past V1, see the
architecture plan).

The final score is NEVER client-supplied -- completing an attempt calls
assessment_run_service.get_result, the same function every other assessment flow already
uses, so there is no "invalid score" input surface to defend against at all; the only client
input at completion is time_taken_seconds (purely informational/display, validated
non-negative). Whether an attempt timed out is likewise never taken on the client's word --
it's computed server-side from ArenaAttempt.created_at vs. the challenge's own
time_limit_seconds, immune to a client's local clock or a fabricated "time's up" call.

Completion is idempotent via the same atomic conditional-UPDATE pattern as
mission_service.complete/subscription_service._activate_paid: score, timed_out, and the
personal-best comparison are all persisted directly on the ArenaAttempt row, so a duplicate
or racing completion call always returns the exact result the first successful call produced,
never a freshly (and possibly differently) recomputed one.
"""

import datetime
from typing import List, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.arena_attempt import ArenaAttempt
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.schemas.arena import (
    ArenaAttemptHistoryItem,
    ArenaAttemptResultRead,
    ArenaAttemptStartRead,
    ArenaChallengeHistoryRead,
    ArenaChallengeRead,
)
from app.services import assessment_run_service
from app.services.arena.challenges import get_challenge, list_challenges


class ArenaError(Exception):
    pass


class ChallengeNotFoundError(ArenaError):
    """Unknown challenge_key."""


class ArenaAttemptNotFoundError(ArenaError):
    """Attempt not found, or not owned by this profile."""


class ArenaAttemptNotReadyError(ArenaError):
    """Raised when completion is requested before every question has been answered and the
    time limit hasn't actually elapsed yet -- there is nothing real to score."""


def _personal_best(db: Session, profile_id: int, challenge_key: str, exclude_id: Optional[int] = None) -> Optional[ArenaAttempt]:
    query = (
        db.query(ArenaAttempt)
        .filter(
            ArenaAttempt.profile_id == profile_id,
            ArenaAttempt.challenge_key == challenge_key,
            ArenaAttempt.status == "completed",
        )
    )
    if exclude_id is not None:
        query = query.filter(ArenaAttempt.id != exclude_id)
    return query.order_by(ArenaAttempt.score.desc()).first()


def list_challenges_for_profile(db: Session, profile_id: int) -> List[ArenaChallengeRead]:
    reads = []
    for challenge in list_challenges():
        best = _personal_best(db, profile_id, challenge.key)
        reads.append(
            ArenaChallengeRead(
                key=challenge.key, title=challenge.title, category=challenge.category,
                description=challenge.description, question_count=challenge.question_count,
                time_limit_seconds=challenge.time_limit_seconds,
                personal_best_score=best.score if best else None,
            )
        )
    return reads


def start_attempt(db: Session, profile_id: int, challenge_key: str) -> ArenaAttemptStartRead:
    challenge = get_challenge(challenge_key)
    if challenge is None:
        raise ChallengeNotFoundError(f"Unknown challenge: {challenge_key}")

    blueprint = AssessmentBlueprint(
        role_id=None,
        jd_id=None,
        profile_id=profile_id,
        source="arena",
        sections=[
            {
                "skill": challenge.skill, "assessment_type": challenge.assessment_type, "weight": 1.0,
                "question_count": challenge.question_count, "is_gap": False,
            }
        ],
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)

    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id, status="in_progress")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)

    attempt = ArenaAttempt(profile_id=profile_id, challenge_key=challenge_key, assessment_id=assessment.id)
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    return ArenaAttemptStartRead(
        arena_attempt_id=attempt.id, assessment_id=assessment.id, challenge_key=challenge_key,
        time_limit_seconds=challenge.time_limit_seconds,
    )


def _get_owned_attempt(db: Session, profile_id: int, arena_attempt_id: int) -> ArenaAttempt:
    attempt = db.get(ArenaAttempt, arena_attempt_id)
    if attempt is None or attempt.profile_id != profile_id:
        raise ArenaAttemptNotFoundError("Arena attempt not found")
    return attempt


def _attempt_result_read(attempt: ArenaAttempt) -> ArenaAttemptResultRead:
    return ArenaAttemptResultRead(
        arena_attempt_id=attempt.id, score=attempt.score, timed_out=attempt.timed_out,
        is_personal_best=bool(attempt.is_personal_best), previous_best_score=attempt.previous_best_score,
    )


def complete_attempt(
    db: Session, profile_id: int, arena_attempt_id: int, time_taken_seconds: int
) -> ArenaAttemptResultRead:
    attempt = _get_owned_attempt(db, profile_id, arena_attempt_id)

    if attempt.status == "completed":
        return _attempt_result_read(attempt)  # already completed -- idempotent no-op

    challenge = get_challenge(attempt.challenge_key)
    now = datetime.datetime.now(datetime.timezone.utc)
    created_at = attempt.created_at if attempt.created_at.tzinfo is not None else attempt.created_at.replace(
        tzinfo=datetime.timezone.utc
    )
    elapsed_seconds = (now - created_at).total_seconds()
    timed_out = challenge is not None and elapsed_seconds > challenge.time_limit_seconds

    assessment = db.get(Assessment, attempt.assessment_id)
    if assessment.status != "completed":
        if not timed_out:
            raise ArenaAttemptNotReadyError("Answer every question, or wait for the timer to run out, first.")
        assessment.status = "completed"
        db.commit()

    result = assessment_run_service.get_result(db, profile_id, assessment.id)

    previous_best = _personal_best(db, profile_id, attempt.challenge_key, exclude_id=attempt.id)
    is_personal_best = previous_best is None or result.overall_score > previous_best.score

    db.execute(
        update(ArenaAttempt)
        .where(ArenaAttempt.id == attempt.id, ArenaAttempt.status != "completed")
        .values(
            status="completed",
            score=result.overall_score,
            time_taken_seconds=time_taken_seconds,
            timed_out=timed_out,
            is_personal_best=is_personal_best,
            previous_best_score=(previous_best.score if previous_best else None),
            completed_at=now,
        )
    )
    db.commit()
    db.refresh(attempt)
    return _attempt_result_read(attempt)


def get_history(db: Session, profile_id: int, challenge_key: str) -> ArenaChallengeHistoryRead:
    if get_challenge(challenge_key) is None:
        raise ChallengeNotFoundError(f"Unknown challenge: {challenge_key}")

    attempts = (
        db.query(ArenaAttempt)
        .filter(
            ArenaAttempt.profile_id == profile_id,
            ArenaAttempt.challenge_key == challenge_key,
            ArenaAttempt.status == "completed",
        )
        .order_by(ArenaAttempt.completed_at.desc())
        .all()
    )
    best_score = max((a.score for a in attempts), default=None)

    return ArenaChallengeHistoryRead(
        challenge_key=challenge_key,
        personal_best_score=best_score,
        attempts=[
            ArenaAttemptHistoryItem(
                arena_attempt_id=a.id, score=a.score, time_taken_seconds=a.time_taken_seconds,
                timed_out=a.timed_out,
                # Recomputed against the current overall best, NOT the persisted
                # attempt.is_personal_best (that field means "was this a new best at the
                # moment it was completed" -- the right semantic for a completion screen's
                # celebratory message, but stale here: a later, better attempt must demote an
                # older one in this list, which the persisted flag deliberately never does).
                is_personal_best=(a.score == best_score),
                completed_at=a.completed_at,
            )
            for a in attempts
        ],
    )
