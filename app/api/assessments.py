from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.assessment import (
    AnswerResult,
    AssessmentBlueprintRead,
    AssessmentRead,
    AssessmentResultRead,
    NextQuestionResponse,
    PrepareMeRequest,
    StartAssessmentRequest,
    SubmitAnswerRequest,
)
from app.services import assessment_run_service as run_service
from app.services.assessment_service import InvalidPrepareMeRequestError, generate_blueprint
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/assessments", tags=["assessments"])


@router.post("/blueprint", response_model=AssessmentBlueprintRead, status_code=status.HTTP_201_CREATED)
def create_blueprint(
    payload: PrepareMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssessmentBlueprintRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return generate_blueprint(db, profile.id, payload.role_id, payload.jd_id)
    except InvalidPrepareMeRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("", response_model=AssessmentRead, status_code=status.HTTP_201_CREATED)
def start_assessment(
    payload: StartAssessmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssessmentRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return run_service.start_assessment(db, profile.id, payload.blueprint_id)
    except run_service.AssessmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


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
