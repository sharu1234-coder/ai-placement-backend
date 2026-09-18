from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.api.upload_utils import read_upload_limited
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.resume import ResumeAnalysisRead, ResumeSummaryRead, ResumeUploadResponse
from app.schemas.resume_ats_check import AtsCheckListItemRead, AtsCheckReportRead, CreateAtsCheckRequest
from app.services.ats_check_service import generate_ats_check, get_ats_check, list_ats_checks
from app.services.profile_service import get_or_create_profile
from app.services.resume_service import (
    ResumeFileUnavailable,
    ResumeProcessingError,
    delete_resume,
    get_latest_resume,
    get_resume_analysis,
    get_resume_file,
    process_resume_upload,
)
from app.storage.provider import StorageProvider, get_storage_provider

router = APIRouter(prefix="/resumes", tags=["resumes"])

_ALLOWED_CONTENT_TYPES = {"application/pdf"}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB


@router.post("", response_model=ResumeUploadResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
    storage: StorageProvider = Depends(get_storage_provider),
) -> ResumeUploadResponse:
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF uploads are supported",
        )

    file_bytes = await read_upload_limited(file, _MAX_UPLOAD_BYTES, "File too large (max 10MB)")

    # The Content-Type check above only looked at a client-supplied header, which any client
    # can set to anything -- this checks the actual bytes. Deliberately just the PDF magic
    # number (not a deeper structural/malware scan, which would need libmagic or an external
    # scanning service -- out of scope here, see README's security-hardening entry).
    if not file_bytes.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File does not look like a valid PDF",
        )

    profile = get_or_create_profile(db, current_user)

    try:
        # process_resume_upload does blocking PDF parsing + a blocking AI call; this route
        # is async (needed for `await file.read()`), and an async route runs directly on
        # the event loop rather than being auto-threadpooled the way a plain `def` route
        # is -- see the identical fix in app/api/interviews.py's submit_audio_answer for
        # the full explanation (found via live testing: a concurrent request froze while
        # this ran uncovered).
        return await run_in_threadpool(
            process_resume_upload,
            db,
            profile.id,
            file.filename or "resume.pdf",
            file_bytes,
            ai_provider,
            storage,
        )
    except ResumeProcessingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/me/latest", response_model=Optional[ResumeSummaryRead])
def get_my_latest_resume(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Optional[ResumeSummaryRead]:
    profile = get_or_create_profile(db, current_user)
    return get_latest_resume(db, profile.id)


@router.get("/ats-checks", response_model=list[AtsCheckListItemRead])
def list_ats_checks_endpoint(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[AtsCheckListItemRead]:
    profile = get_or_create_profile(db, current_user)
    return list_ats_checks(db, profile.id)


@router.get("/ats-checks/{check_id}", response_model=AtsCheckReportRead)
def get_ats_check_endpoint(
    check_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> AtsCheckReportRead:
    profile = get_or_create_profile(db, current_user)
    report = get_ats_check(db, check_id, profile.id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ATS check not found")
    return report


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


@router.get("/{resume_id}/file")
def get_resume_file_endpoint(
    resume_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> Response:
    """Stream back the student's own uploaded PDF so the app can preview it.

    Deliberately NOT a redirect to a storage URL: a pre-signed S3 link would be a bearer
    credential of its own, valid for anyone who obtained it for as long as it lived. Serving the
    bytes through this authenticated route keeps the existing token as the only way in.

    Returned inline rather than as an attachment -- the caller is an in-app viewer, not a
    browser download -- and the filename is quoted defensively because it is student-supplied.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        result = get_resume_file(db, resume_id, profile.id, storage)
    except ResumeFileUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Resume file is temporarily unavailable",
        )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")

    data, filename = result
    # Strip anything that could break out of the quoted header value or inject a second header.
    _UNSAFE_IN_HEADER = '"\\;\r\n'
    safe_name = "".join(ch for ch in filename if ch.isprintable() and ch not in _UNSAFE_IN_HEADER)[:120]
    safe_name = safe_name or "resume.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            # A resume is personal data -- keep it out of shared caches entirely.
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/{resume_id}/ats-check", response_model=AtsCheckReportRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
def create_ats_check_endpoint(
    request: Request,
    resume_id: int,
    payload: CreateAtsCheckRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
    storage: StorageProvider = Depends(get_storage_provider),
) -> AtsCheckReportRead:
    profile = get_or_create_profile(db, current_user)
    report = generate_ats_check(db, profile.id, resume_id, payload.target_role, ai_provider, storage)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
    return report


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resume_endpoint(
    resume_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> None:
    profile = get_or_create_profile(db, current_user)
    if not delete_resume(db, resume_id, profile.id, storage):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
