from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.interview import (
    InterviewAnswerRead,
    InterviewEvaluationRead,
    InterviewRead,
    NextInterviewQuestionResponse,
    StartInterviewRequest,
    SubmitInterviewAnswerRequest,
)
from app.services import interview_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/interviews", tags=["interviews"])


@router.post("", response_model=InterviewRead, status_code=status.HTTP_201_CREATED)
def start_interview(
    payload: StartInterviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return interview_service.start_interview(db, profile.id, payload.type, payload.mode, payload.jd_id)
    except interview_service.InvalidInterviewRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/{interview_id}/next-question", response_model=NextInterviewQuestionResponse)
def next_question(
    interview_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> NextInterviewQuestionResponse:
    profile = get_or_create_profile(db, current_user)
    try:
        return interview_service.get_next_question(db, profile.id, interview_id, ai_provider)
    except interview_service.InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{interview_id}/answer", response_model=InterviewAnswerRead)
def submit_answer(
    interview_id: int,
    payload: SubmitInterviewAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewAnswerRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return interview_service.submit_answer(
            db, profile.id, interview_id, payload.interview_question_id, payload.answer_text
        )
    except interview_service.InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except interview_service.InterviewQuestionNotPendingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{interview_id}/answer/audio", status_code=status.HTTP_501_NOT_IMPLEMENTED)
def submit_audio_answer(interview_id: int, current_user: User = Depends(get_current_user)) -> None:
    """Route exists to match the documented API surface (section 8.5); voice interviews
    (turn-based STT/LLM/TTS) are Stage 10 scope, not Stage 9."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Voice interview answers aren't available yet (Stage 10)",
    )


@router.get("/{interview_id}/evaluation", response_model=InterviewEvaluationRead)
def get_evaluation(
    interview_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> InterviewEvaluationRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return interview_service.get_evaluation(db, profile.id, interview_id, ai_provider)
    except interview_service.InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except interview_service.InterviewNotCompleteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
