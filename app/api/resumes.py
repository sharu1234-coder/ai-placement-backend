from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.api.upload_utils import read_upload_limited
from app.models.user import User
from app.schemas.resume import ResumeAnalysisRead, ResumeUploadResponse
from app.services.profile_service import get_or_create_profile
from app.services.resume_service import (
    ResumeProcessingError,
    delete_resume,
    get_resume_analysis,
    process_resume_upload,
)

router = APIRouter(prefix="/resumes", tags=["resumes"])

_ALLOWED_CONTENT_TYPES = {"application/pdf"}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB


@router.post("", response_model=ResumeUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> ResumeUploadResponse:
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF uploads are supported",
        )

    file_bytes = await read_upload_limited(file, _MAX_UPLOAD_BYTES, "File too large (max 10MB)")

    profile = get_or_create_profile(db, current_user)

    try:
        # process_resume_upload does blocking PDF parsing + a blocking AI call; this route
        # is async (needed for `await file.read()`), and an async route runs directly on
        # the event loop rather than being auto-threadpooled the way a plain `def` route
        # is -- see the identical fix in app/api/interviews.py's submit_audio_answer for
        # the full explanation (found via live testing: a concurrent request froze while
        # this ran uncovered).
        return await run_in_threadpool(
            process_resume_upload, db, profile.id, file.filename or "resume.pdf", file_bytes, ai_provider
        )
    except ResumeProcessingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/{resume_id}/analysis", response_model=ResumeAnalysisRead)
def get_resume_analysis_endpoint(
    resume_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResumeAnalysisRead:
    profile = get_or_create_profile(db, current_user)
    analysis = get_resume_analysis(db, resume_id, profile.id)
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume analysis not found")
    return analysis


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resume_endpoint(
    resume_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    profile = get_or_create_profile(db, current_user)
    if not delete_resume(db, resume_id, profile.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
