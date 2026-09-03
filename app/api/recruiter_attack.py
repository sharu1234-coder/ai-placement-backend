from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
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
from app.services import recruiter_attack_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/recruiter-attack", tags=["recruiter-attack"])


@router.post("/sessions", response_model=RecruiterAttackSessionRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
def start_session(
    request: Request,
    payload: StartRecruiterAttackSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RecruiterAttackSessionRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return recruiter_attack_service.start_session(
            db, profile, payload.source_type, payload.project_build_id, payload.claim_text
        )
    except recruiter_attack_service.InvalidSourceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


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
