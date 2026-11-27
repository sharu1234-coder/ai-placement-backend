import logging
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.api.upload_utils import read_upload_limited
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.resume import ResumeAnalysisRead, ResumeSummaryRead, ResumeUploadResponse
from app.schemas.resume_ats_check import AtsCheckListItemRead, AtsCheckReportRead, CreateAtsCheckRequest
from app.services import idempotency
from app.services.ats_check_service import generate_ats_check, get_ats_check, list_ats_checks
from app.services.profile_service import get_or_create_profile
from app.services.resume_service import (
    ResumeErasureFailed,
    ResumeFileUnavailable,
    ResumeProcessingError,
    ResumeStorageUnconfirmed,
    delete_resume,
    get_latest_resume,
    get_resume_analysis,
    get_resume_file,
    process_resume_upload,
    read_uploaded_resume,
)
from app.storage.erasure import IMMEDIATE_ATTENTION_CODES, PROTECTED_ERROR_CODES
from app.storage.identity import IDENTITY_FAILURES
from app.storage.provider import StorageProvider, get_storage_provider

logger = logging.getLogger(__name__)

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
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
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

    async def perform() -> tuple[object, int]:
        try:
            # process_resume_upload does blocking PDF parsing + a blocking AI call; this route
            # is async (needed for `await file.read()`), and an async route runs directly on
            # the event loop rather than being auto-threadpooled the way a plain `def` route
            # is -- see the identical fix in app/api/interviews.py's submit_audio_answer for
            # the full explanation (found via live testing: a concurrent request froze while
            # this ran uncovered).
            created = await run_in_threadpool(
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
        except ResumeStorageUnconfirmed:
            # 500 rather than 422: the upload is fine and the student can do nothing about it.
            # Nothing was stored -- the destination is captured before the save precisely so
            # that a provider which cannot say where it writes does not leave an object behind.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "The resume could not be stored because the storage destination could "
                    "not be confirmed"
                ),
            )
        return created, created.id

    try:
        # The fingerprint is a digest of the file, never the file. A resume is the largest body
        # this API accepts and the most sensitive thing a student sends, so storing it in a
        # deduplication table would be both wasteful and a liability; 64 hex characters identify
        # the same upload just as well. Re-uploading a *different* resume under the same key is
        # then a conflict rather than a silent no-op, which is the honest answer.
        result = await idempotency.run_once_async(
            db,
            profile.id,
            operation="resume.upload",
            idempotency_key=idempotency_key,
            params={
                "filename": file.filename or "resume.pdf",
                "body": idempotency.body_digest(file_bytes),
            },
            perform=perform,
            read_back=lambda rid: read_uploaded_resume(db, profile.id, rid),
            result_kind="resume",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "resume.upload")
    return result


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
    except ResumeStorageUnconfirmed:
        # Explicitly not the 503 below, and explicitly not a 404. 503 would promise it clears
        # by itself; a 404 would tell the student their resume is gone when the row and very
        # likely the object are both still there. This resume cannot be read until an operator
        # points the service back at the storage that has it, or recovers the identity.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_UNCONFIRMED_DETAIL
        )
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
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
    storage: StorageProvider = Depends(get_storage_provider),
) -> AtsCheckReportRead:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[AtsCheckReportRead, int]:
        report = generate_ats_check(
            db, profile.id, resume_id, payload.target_role, ai_provider, storage
        )
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
        return report, report.id

    try:
        # A paid, server-side AI analysis that writes a report row. Phase 24 found the screen's
        # own rotation guard covers the common case but not process death; this closes the
        # window underneath it, where the transport replays a request that already committed.
        return idempotency.run_once(
            db,
            profile.id,
            operation="resume.ats_check",
            idempotency_key=idempotency_key,
            params={"resume_id": resume_id, "target_role": payload.target_role},
            perform=perform,
            read_back=lambda rid: get_ats_check(db, rid, profile.id),
            result_kind="resume_ats_check",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "resume.ats_check")


#: One body for every unconfirmable destination, on purpose. The student cannot act on which
#: of the six refusals it was, and the codes name configuration -- a bucket that moved, an
#: owner guard that is no longer asserted -- so the detail belongs in the log, which carries
#: the resume id and the code instead.
_UNCONFIRMED_DETAIL = (
    "The storage holding this resume could not be confirmed, so the request was refused"
)


#: Erasure failures that need an operator, not a retry: a missing bucket, a provider that
#: cannot enumerate versions or cannot erase them, a provider that cannot say where it points,
#: a refused authorization. Reported as 500 rather than 503 because "try again later" would be
#: false -- none of these clears without somebody changing a configuration or a policy. A
#: retention hold is excluded: it is answered separately, because it is the storage behaving
#: correctly rather than a fault.
_OPERATOR_FAULT_CODES = (
    IMMEDIATE_ATTENTION_CODES - PROTECTED_ERROR_CODES
) | IDENTITY_FAILURES


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resume_endpoint(
    resume_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> None:
    """Delete a resume and permanently erase its stored file.

    204 now means what it says. Before Stage B.1 this endpoint answered 204 whenever the
    database rows went, including when the file could not be deleted at all -- and on a
    versioned bucket, including when the "delete" had only inserted a delete marker and left
    every byte in place. It now answers 204 only after the provider has verified that no
    version and no delete marker of the object remains.

    The failure answers all share one property: **the resume is still there.** Nothing was
    deleted, so the client's list is still correct and the request can simply be repeated.

    * 409 -- an Object Lock retention or a legal hold covers the file. The storage is doing
      exactly what it was configured to do, so this is a conflict between two obligations and
      not a fault; it needs whoever owns the retention policy, and no amount of retrying moves
      it. Phrased for the student, because this is the one failure they may be told about.
    * 500 -- a misconfiguration or a refused authorization on our side (see
      `_OPERATOR_FAULT_CODES`). Not 503: nothing about it is temporary.
    * 503 -- anything else, which in practice is a throttle, a transport fault, or a version
      arriving from a concurrent write between the delete and the verification. These do clear
      by themselves, so "try again" is honest.

    None of the details reach the response. A bucket name, an endpoint, an account id or a
    storage key would each be a configuration detail in a student-facing body, and the key
    itself is a user-scoped path.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        deleted = delete_resume(db, resume_id, profile.id, storage)
    except ResumeStorageUnconfirmed:
        # The resume, its derived rows and its file are all untouched: the check runs before
        # the erasure, which runs before any row is deleted.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_UNCONFIRMED_DETAIL
        )
    except ResumeErasureFailed as exc:
        # The code is the whole diagnosis that crosses this boundary. `exc` is logged by the
        # service, which has the resume id; re-logging the message here could carry a bucket.
        if exc.code in PROTECTED_ERROR_CODES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This resume's file is under a retention hold and cannot be permanently "
                    "deleted yet. The resume has not been removed."
                ),
            ) from exc
        if exc.code in _OPERATOR_FAULT_CODES:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "The resume could not be permanently deleted because of a storage "
                    "configuration problem. The resume has not been removed."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The resume's file could not be permanently deleted just now. The resume has "
                "not been removed -- please try again."
            ),
        ) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
