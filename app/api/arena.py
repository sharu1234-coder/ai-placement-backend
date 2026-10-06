import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.arena import (
    ArenaAttemptResultRead,
    ArenaAttemptStartRead,
    ArenaChallengeHistoryRead,
    ArenaChallengeRead,
    CompleteArenaAttemptRequest,
)
from app.services import arena_service, idempotency
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/arena", tags=["arena"])


@router.get("/challenges", response_model=List[ArenaChallengeRead])
def list_challenges(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> List[ArenaChallengeRead]:
    profile = get_or_create_profile(db, current_user)
    return arena_service.list_challenges_for_profile(db, profile.id)


@router.post("/challenges/{challenge_key}/attempt", response_model=ArenaAttemptStartRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/hour")
def start_attempt(
    request: Request,
    challenge_key: str,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ArenaAttemptStartRead:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[ArenaAttemptStartRead, int]:
        try:
            created = arena_service.start_attempt(db, profile.id, challenge_key)
        except arena_service.ChallengeNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        return created, created.arena_attempt_id

    try:
        # Three rows per call -- a blueprint, an assessment and the attempt -- so a replay is
        # the most expensive duplicate of the non-AI endpoints, and leaves a timed challenge
        # the student never started.
        return idempotency.run_once(
            db,
            profile.id,
            operation="arena.start_attempt",
            idempotency_key=idempotency_key,
            params={"challenge_key": challenge_key},
            perform=perform,
            read_back=lambda rid: arena_service.read_attempt_start(db, profile.id, rid),
            result_kind="arena_attempt",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "arena.start_attempt")


@router.post("/attempts/{arena_attempt_id}/complete", response_model=ArenaAttemptResultRead)
@limiter.limit("30/hour")
def complete_attempt(
    request: Request,
    arena_attempt_id: int,
    payload: CompleteArenaAttemptRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ArenaAttemptResultRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return arena_service.complete_attempt(db, profile.id, arena_attempt_id, payload.time_taken_seconds)
    except arena_service.ArenaAttemptNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except arena_service.ArenaAttemptNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/challenges/{challenge_key}/history", response_model=ArenaChallengeHistoryRead)
def get_history(
    challenge_key: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> ArenaChallengeHistoryRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return arena_service.get_history(db, profile.id, challenge_key)
    except arena_service.ChallengeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
