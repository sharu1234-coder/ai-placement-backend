import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.api.ai_errors import ai_unavailable
from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.assessment import (
    AnswerResult,
    AssessmentBlueprintRead,
    AssessmentHistoryItem,
    AssessmentRead,
    AssessmentResultRead,
    NextQuestionResponse,
    PrepareMeRequest,
    StartAssessmentRequest,
    SubmitAnswerRequest,
)
from app.services import assessment_run_service as run_service
from app.services import idempotency
from app.services.assessment_service import (
    InvalidPrepareMeRequestError,
    generate_blueprint,
    get_assessment_history,
    read_blueprint,
)
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assessments", tags=["assessments"])


@router.post("/blueprint", response_model=AssessmentBlueprintRead, status_code=status.HTTP_201_CREATED)
def create_blueprint(
    payload: PrepareMeRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssessmentBlueprintRead:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[AssessmentBlueprintRead, int]:
        try:
            created = generate_blueprint(
                db, profile.id, payload.role_id, payload.jd_id, payload.category
            )
        except InvalidPrepareMeRequestError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        return created, created.id

    try:
        # generate_blueprint inserts a blueprint per call, and the Test Center creates one on
        # the way into every test, so this is the endpoint a replayed tap hits first.
        return idempotency.run_once(
            db,
            profile.id,
            operation="assessment.blueprint",
            idempotency_key=idempotency_key,
            params={
                "role_id": payload.role_id,
                "jd_id": payload.jd_id,
                "category": payload.category,
            },
            perform=perform,
            read_back=lambda rid: read_blueprint(db, profile.id, rid),
            result_kind="assessment_blueprint",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "assessment.blueprint")


@router.get("/history", response_model=list[AssessmentHistoryItem])
def get_history(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[AssessmentHistoryItem]:
    profile = get_or_create_profile(db, current_user)
    return get_assessment_history(db, profile.id)


@router.post("", response_model=AssessmentRead, status_code=status.HTTP_201_CREATED)
def start_assessment(
    payload: StartAssessmentRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssessmentRead:
    """Start an assessment, exactly once per logical request.

    start_assessment() inserts unconditionally -- no natural key, nothing to collapse a
    duplicate against -- and the client's transport is free to send this twice when a response
    is lost. The guard is what makes the second arrival return the first one's assessment
    instead of creating a second half-finished one.
    """
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[AssessmentRead, int]:
        try:
            created = run_service.start_assessment(db, profile.id, payload.blueprint_id)
        except run_service.AssessmentNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        return created, created.id

    try:
        return idempotency.run_once(
            db,
            profile.id,
            operation="assessment.start",
            idempotency_key=idempotency_key,
            params={"blueprint_id": payload.blueprint_id},
            perform=perform,
            read_back=lambda rid: run_service.read_assessment(db, profile.id, rid),
            result_kind="assessment",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "assessment.start")


@router.get("/{assessment_id}/next-question", response_model=NextQuestionResponse)
@limiter.limit("60/hour")
def next_question(
    request: Request,
    assessment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> NextQuestionResponse:
    profile = get_or_create_profile(db, current_user)
    try:
        return run_service.get_next_question(db, profile.id, assessment_id, ai_provider)
    except run_service.AssessmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except AIGenerationError as exc:
        raise ai_unavailable(exc, logger, "assessments")


@router.post("/{assessment_id}/answer", response_model=AnswerResult)
@limiter.limit("60/hour")
def submit_answer(
    request: Request,
    assessment_id: int,
    payload: SubmitAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> AnswerResult:
    profile = get_or_create_profile(db, current_user)
    try:
        return run_service.submit_answer(
            db,
            profile.id,
            assessment_id,
            payload.question_id,
            payload.student_answer,
            payload.time_taken_seconds,
            ai_provider,
        )
    except run_service.AssessmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except run_service.QuestionNotPendingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except AIGenerationError as exc:
        raise ai_unavailable(exc, logger, "assessments")


@router.get("/{assessment_id}/result", response_model=AssessmentResultRead)
def get_result(
    assessment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssessmentResultRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return run_service.get_result(db, profile.id, assessment_id)
    except run_service.AssessmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except run_service.AssessmentNotCompleteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
