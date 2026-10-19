import logging
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.api.ai_deps import requires_synthesize, requires_transcribe
from app.api.ai_errors import ai_unavailable
from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.api.upload_utils import read_upload_limited
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.interview import (
    InterviewAnswerRead,
    InterviewEvaluationRead,
    InterviewHistoryItem,
    InterviewRead,
    NextInterviewQuestionResponse,
    StartInterviewRequest,
    SubmitInterviewAnswerRequest,
    SubmitSpokenTurnRequest,
)
from app.services import idempotency, interview_service, voice_interview
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/interviews", tags=["interviews"])


@router.get("", response_model=list[InterviewHistoryItem])
def list_interviews(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[InterviewHistoryItem]:
    profile = get_or_create_profile(db, current_user)
    return interview_service.list_interviews(db, profile.id)

_MAX_AUDIO_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
# Matches what the Android client actually records and sends (MediaRecorder with
# OutputFormat.MPEG_4 + AudioEncoder.AAC, sent as "audio/mp4" -- see
# InterviewRepositoryImpl.kt). This had no validation at all before -- any content-type
# (or none) was accepted.
_ALLOWED_AUDIO_CONTENT_TYPES = {"audio/mp4", "audio/m4a", "audio/x-m4a"}


@router.post("", response_model=InterviewRead, status_code=status.HTTP_201_CREATED)
def start_interview(
    payload: StartInterviewRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewRead:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[InterviewRead, int]:
        try:
            created = interview_service.start_interview(
                db, profile.id, payload.type, payload.mode, payload.jd_id
            )
        except interview_service.InvalidInterviewRequestError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        return created, created.id

    try:
        # start_interview inserts unconditionally, so a replayed start would leave a second
        # half-finished interview in the student's history for one tap.
        return idempotency.run_once(
            db,
            profile.id,
            operation="interview.start",
            idempotency_key=idempotency_key,
            params={"type": payload.type, "mode": payload.mode, "jd_id": payload.jd_id},
            perform=perform,
            read_back=lambda rid: interview_service.read_interview(db, profile.id, rid),
            result_kind="interview",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "interview.start")


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
    # This route generates a question through the AI provider, so it can raise
    # AIGenerationError -- and until this was added it had no handler, unlike its direct
    # counterpart in assessments.py. A provider outage mid-interview therefore surfaced as an
    # unhandled 500 rather than the documented 422 the Android client knows how to show.
    except AIGenerationError as exc:
        raise ai_unavailable(exc, logger, "interviews")


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
    ai_provider: AIProvider = Depends(requires_transcribe),
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
        raise ai_unavailable(exc, logger, "interviews")


@router.get("/{interview_id}/questions/{question_id}/audio")
@limiter.limit("60/hour")
def get_question_audio(
    request: Request,
    interview_id: int,
    question_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(requires_synthesize),
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
        raise ai_unavailable(exc, logger, "interviews")

    return Response(content=audio_bytes, media_type="audio/wav")


@router.post("/{interview_id}/voice-turn", response_model=None)
@limiter.limit("60/hour")
def submit_spoken_turn(
    request: Request,
    interview_id: int,
    payload: SubmitSpokenTurnRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> dict:
    """VA-9: store one spoken answer and return the next question, in one round trip.

    Composes the two calls a voice client would otherwise make back to back -- POST /answer then
    GET /next-question -- so that the window between them cannot leave a client holding a stored
    answer it believes failed. Everything authoritative still happens in `interview_service`:
    this route adds no scoring, no progression rule and no second state machine.

    Why not an audio upload: POST /answer/audio already does that, and duplicating it here would
    be a second transcription path to keep in step. This route takes the transcript a realtime
    voice turn already has, which also buys the engine's byte-exact replay detection -- the audio
    path's replay identity is weaker by its own documented admission.

    Why no Idempotency-Key: the interview engine is already the deduplicator. A question takes
    one answer, enforced by a unique index rather than a check-then-act read, and
    `_is_replay_of` decides whether a second arrival is a resend or a different answer. An
    operation claim on top would be a second mechanism that could disagree with the first.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        turn = voice_interview.submit_spoken_turn(
            db, profile.id, interview_id, payload.interview_question_id, payload.transcript, ai_provider
        )
    except interview_service.InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        # An empty or oversized transcript. 422 rather than storing it: the first answer to a
        # question is final, so spending it on silence is not recoverable.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except AIGenerationError as exc:
        # Only reachable from a provider failure outside the next-question call, which
        # `submit_spoken_turn` already absorbs into a state. Mapped anyway so a future change
        # there cannot turn into a 500.
        raise ai_unavailable(exc, logger, "interviews")

    return turn.to_payload()


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
    # Same gap as next-question above, and this one lands at the worst moment: the student has
    # finished the whole interview and is asking for their result. An AI failure here used to
    # escape as a 500.
    except AIGenerationError as exc:
        raise ai_unavailable(exc, logger, "interviews")


@router.delete("/{interview_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/hour")
def delete_interview(
    request: Request,
    interview_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Let a student delete one of their own interviews, with its answers and evaluation.

    Answers are kept for review by design, so this is the counterpart to that: the student
    decides when a transcript goes. There is no automatic expiry, and no AI provider is
    involved, so this route takes no provider dependency.

    404 rather than 403 for someone else's interview -- the same choice every other interview
    route makes, so the response cannot be used to learn that an id exists.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        interview_service.delete_interview(db, profile.id, interview_id)
    except interview_service.InterviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
