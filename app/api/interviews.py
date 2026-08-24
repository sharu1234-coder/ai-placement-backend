from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.api.upload_utils import read_upload_limited
from app.core.rate_limit import limiter
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

_MAX_AUDIO_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
# Matches what the Android client actually records and sends (MediaRecorder with
# OutputFormat.MPEG_4 + AudioEncoder.AAC, sent as "audio/mp4" -- see
# InterviewRepositoryImpl.kt). This had no validation at all before -- any content-type
# (or none) was accepted.
_ALLOWED_AUDIO_CONTENT_TYPES = {"audio/mp4", "audio/m4a", "audio/x-m4a"}


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
@limiter.limit("60/hour")
def next_question(
    request: Request,
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


@router.post("/{interview_id}/answer/audio", response_model=InterviewAnswerRead)
@limiter.limit("60/hour")
async def submit_audio_answer(
    request: Request,
    interview_id: int,
    interview_question_id: int = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> InterviewAnswerRead:
    if file.content_type not in _ALLOWED_AUDIO_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported audio format",
        )

    profile = get_or_create_profile(db, current_user)
    audio_bytes = await read_upload_limited(file, _MAX_AUDIO_UPLOAD_BYTES, "Audio file too large (max 10MB)")

    # Same reasoning as resumes.py's %PDF- check: the Content-Type header above is
    # client-supplied and spoofable, so this checks the actual bytes too. MP4/M4A containers
    # (ISO base media file format) start with a box whose size is the first 4 bytes and whose
    # type is the next 4 -- for a well-formed file that first box is almost always "ftyp".
    if audio_bytes[4:8] != b"ftyp":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File does not look like a valid audio recording",
        )

    try:
        # transcribe() is a slow, blocking, synchronous call (local model inference); this
        # route is async (needed for `await file.read()`), and an async route runs directly
        # on the event loop -- unlike a sync `def` route, FastAPI does NOT auto-threadpool
        # it, so calling a blocking function here would freeze every other request on the
        # server for the duration. run_in_threadpool avoids that.
        return await run_in_threadpool(
            interview_service.submit_audio_answer,
            db, profile.id, interview_id, interview_question_id, audio_bytes, ai_provider,
        )
    except interview_service.InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except interview_service.InterviewQuestionNotPendingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except AIGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/{interview_id}/questions/{question_id}/audio")
@limiter.limit("60/hour")
def get_question_audio(
    request: Request,
    interview_id: int,
    question_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> Response:
    """Not in the documented endpoint table (section 8.5) -- added because turn-based voice
    needs the system to "speak" each question, and the doc's own AIProvider.synthesize_speech
    only makes sense with a route that calls it. Synthesized on demand, never persisted, to
    honor "minimize stored voice data"."""
    profile = get_or_create_profile(db, current_user)
    try:
        audio_bytes = interview_service.synthesize_question_audio(
            db, profile.id, interview_id, question_id, ai_provider
        )
    except interview_service.InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except AIGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return Response(content=audio_bytes, media_type="audio/wav")


@router.get("/{interview_id}/evaluation", response_model=InterviewEvaluationRead)
@limiter.limit("60/hour")
def get_evaluation(
    request: Request,
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
