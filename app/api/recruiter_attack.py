import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.recruiter_attack import (
    NextRecruiterAttackTurnResponse,
    RecruiterAttackResultsRead,
    RecruiterAttackSessionListItem,
    RecruiterAttackSessionRead,
    RecruiterAttackTurnRead,
    StartRecruiterAttackSessionRequest,
    SubmitRecruiterAttackAnswerRequest,
)
from app.services import idempotency, recruiter_attack_service
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recruiter-attack", tags=["recruiter-attack"])


@router.post("/sessions", response_model=RecruiterAttackSessionRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
def start_session(
    request: Request,
    payload: StartRecruiterAttackSessionRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RecruiterAttackSessionRead:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[RecruiterAttackSessionRead, int]:
        try:
            created = recruiter_attack_service.start_session(
                db, profile, payload.source_type, payload.project_build_id, payload.claim_text
            )
        except recruiter_attack_service.InvalidSourceError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        return created, created.id

    try:
        # start_session inserts unconditionally, so a replay leaves a second abandoned session
        # in the student's history for one tap.
        return idempotency.run_once(
            db,
            profile.id,
            operation="recruiter_attack.start",
            idempotency_key=idempotency_key,
            params={
                "source_type": payload.source_type,
                "project_build_id": payload.project_build_id,
                "claim_text": payload.claim_text,
            },
            perform=perform,
            read_back=lambda rid: recruiter_attack_service.read_session(db, profile.id, rid),
            result_kind="recruiter_attack_session",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "recruiter_attack.start")


@router.get("/sessions", response_model=List[RecruiterAttackSessionListItem])
def list_sessions(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> List[RecruiterAttackSessionListItem]:
    profile = get_or_create_profile(db, current_user)
    return recruiter_attack_service.list_sessions(db, profile.id)


@router.get("/sessions/{session_id}/next-turn", response_model=NextRecruiterAttackTurnResponse)
@limiter.limit("60/hour")
def next_turn(
    request: Request,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> NextRecruiterAttackTurnResponse:
    profile = get_or_create_profile(db, current_user)
    try:
        return recruiter_attack_service.get_next_turn(db, profile.id, session_id, ai_provider)
    except recruiter_attack_service.RecruiterAttackNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/sessions/{session_id}/answer", response_model=RecruiterAttackTurnRead)
def answer(
    session_id: int,
    payload: SubmitRecruiterAttackAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RecruiterAttackTurnRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return recruiter_attack_service.answer_turn(
            db, profile.id, session_id, payload.turn_id, payload.answer_text
        )
    except recruiter_attack_service.RecruiterAttackNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except recruiter_attack_service.TurnNotPendingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/sessions/{session_id}/results", response_model=RecruiterAttackResultsRead)
@limiter.limit("60/hour")
def results(
    request: Request,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> RecruiterAttackResultsRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return recruiter_attack_service.get_results(db, profile.id, session_id, ai_provider)
    except recruiter_attack_service.RecruiterAttackNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except recruiter_attack_service.RecruiterAttackNotCompleteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
