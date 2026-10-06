import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.api.ai_errors import ai_unavailable
from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.api.upload_utils import read_upload_limited
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.job import (
    GeneratePreparationPlanRequest,
    JobDescriptionListItem,
    JobDescriptionParseResponse,
    JobMatchRead,
    JobPreparationPlanRead,
    ParseJDRequest,
    RequirementRead,
)
from app.services import idempotency
from app.services.jd_service import (
    PREPARATION_PLAN_OPERATION,
    JobDescriptionInUseError,
    delete_job_description,
    ensure_preparation_plan,
    get_preparation_plan,
    get_requirements,
    list_job_descriptions,
    parse_job_description,
    read_parsed_jd,
)
from app.services.matching_service import match_resume_to_job, read_match
from app.services.profile_service import get_or_create_profile
from app.services.resume_service import ResumeProcessingError, extract_text_from_pdf
from app.services.skill_matching import NoResumeAnalysisError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# One budget for parsing one job description, whichever way the text arrives.
#
# Previously both routes carried a plain @limiter.limit("20/hour"). slowapi scopes a decorated
# limit to the endpoint (Limit.scope defaults to the endpoint key), so that was two independent
# buckets: alternating paste and upload gave 40 AI parses an hour, which is exactly the
# doubling the comment on the upload route claimed to prevent. shared_limit with an explicit
# scope is what actually shares a counter between two endpoints.
#
# Deliberately NOT extended to POST /{jd_id}/preparation-plan. That is a different AI operation
# (generating a plan, not extracting requirements from text), it cannot be reached without
# having parsed a JD first, and folding it in here would silently halve the parse allowance to
# pay for it. Grouping endpoints that merely both cost money, rather than ones that are the
# same operation reached two ways, is how a shared budget stops meaning anything.
JD_PARSE_SCOPE = "jd_parse"
JD_PARSE_LIMIT = "20/hour"

_ALLOWED_JD_CONTENT_TYPES = {"application/pdf"}
# Half the resume ceiling: a job description is a page or two, where a resume can legitimately
# carry embedded fonts and images. Smaller limit, same enforcement path.
_MAX_JD_UPLOAD_BYTES = 5 * 1024 * 1024
# Mirrors ParseJDRequest.raw_text's max_length so both entry points refuse at the same size.
_MAX_JD_TEXT_LENGTH = 20_000


@router.get("", response_model=list[JobDescriptionListItem])
def list_jobs(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[JobDescriptionListItem]:
    profile = get_or_create_profile(db, current_user)
    return list_job_descriptions(db, profile.id)


@router.post("", response_model=JobDescriptionParseResponse, status_code=status.HTTP_201_CREATED)
@limiter.shared_limit(JD_PARSE_LIMIT, scope=JD_PARSE_SCOPE)
def create_job_description(
    request: Request,
    payload: ParseJDRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> JobDescriptionParseResponse:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[JobDescriptionParseResponse, int]:
        try:
            parsed = parse_job_description(db, profile.id, payload.raw_text, ai_provider)
        # JD parsing is one of the few AI paths with no template fallback in its service (there
        # is nothing sensible to invent from an unparsed job description), so AIGenerationError
        # reaches the route. Without this it escaped as an unhandled 500, which is the wrong
        # answer twice over: the client cannot distinguish "provider is down, retry" from "the
        # server is broken", and every provider blip logged a full stack trace for an expected
        # condition. Found by driving every AI-taking route with a failing provider; this was
        # the only one of the twenty still returning 500.
        except AIGenerationError as exc:
            raise ai_unavailable(exc, logger, "jobs")
        return parsed, parsed.id

    try:
        # A parse writes a JobDescription plus its Requirement rows and spends a model pass on
        # the text, so a replay leaves an orphaned JD behind and pays twice to read the same
        # posting. Fingerprinted on the text itself, not on a client-chosen title.
        return idempotency.run_once(
            db,
            profile.id,
            operation="jd.parse",
            idempotency_key=idempotency_key,
            params={"raw_text": idempotency.body_digest(payload.raw_text.encode("utf-8"))},
            perform=perform,
            read_back=lambda rid: read_parsed_jd(db, profile.id, rid),
            result_kind="job_description",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "jd.parse")


@router.post("/upload", response_model=JobDescriptionParseResponse, status_code=status.HTTP_201_CREATED)
@limiter.shared_limit(JD_PARSE_LIMIT, scope=JD_PARSE_SCOPE)
async def create_job_description_from_pdf(
    request: Request,
    file: UploadFile = File(...),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> JobDescriptionParseResponse:
    """Analyse a job description the student has as a PDF instead of as text.

    Deliberately the same pipeline as POST /jobs: this route only turns a PDF into text and
    then hands that text to the identical parser, so a JD gets the same hardened prompt, the
    same requirement extraction and the same 20,000-character ceiling however it arrived. The
    PDF is never stored -- unlike a resume, which the student needs back, a JD is only ever
    input to one analysis, so keeping the file would retain third-party data we have no use
    for.

    The extracted text is treated as exactly as untrusted as pasted text. Nothing in it is
    fetched, resolved or executed: URLs inside a JD stay inert strings, and no PDF metadata is
    read as job information, because metadata is attacker-controlled and invisible to the
    student who uploaded it.

    Shares the parse route's rate limit budget deliberately -- both spend the same scarce
    resource (an AI generation), so an attacker must not be able to double their allowance by
    alternating between the two. That sharing is real: both routes use shared_limit against
    JD_PARSE_SCOPE. It was not, before Stage 6 -- a plain @limiter.limit is scoped per endpoint,
    so this route had its own 20/hour and alternating gave 40. See JD_PARSE_SCOPE above and
    tests/unit/test_rate_limit_identity.py, which spends the budget across both paths.
    """
    if file.content_type not in _ALLOWED_JD_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF uploads are supported",
        )

    file_bytes = await read_upload_limited(file, _MAX_JD_UPLOAD_BYTES, "File too large (max 5MB)")

    # The Content-Type above is a client-supplied header and can say anything; this checks the
    # actual bytes, the same way the resume upload does.
    if not file_bytes.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File does not look like a valid PDF",
        )

    try:
        raw_text = await run_in_threadpool(extract_text_from_pdf, file_bytes)
    except ResumeProcessingError as exc:
        # Covers both a PDF that will not open and one with no extractable text (a scan). The
        # message comes from the extractor and describes the file, never the internals.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    # Same ceiling as the pasted path, enforced here rather than left to the parser so an
    # oversized document is refused before it can be embedded in a prompt.
    if len(raw_text) > _MAX_JD_TEXT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"That PDF holds more text than we can analyse (limit {_MAX_JD_TEXT_LENGTH:,} characters).",
        )

    profile = get_or_create_profile(db, current_user)

    async def perform() -> tuple[JobDescriptionParseResponse, int]:
        try:
            parsed = await run_in_threadpool(
                parse_job_description, db, profile.id, raw_text, ai_provider, "pdf_upload"
            )
        except AIGenerationError as exc:
            raise ai_unavailable(exc, logger, "jobs")
        return parsed, parsed.id

    try:
        # Fingerprinted on the extracted text rather than the PDF bytes, deliberately: the same
        # posting re-exported is the same analysis, and the text is what the parser actually
        # receives. A digest either way -- the document itself never reaches the claims table.
        return await idempotency.run_once_async(
            db,
            profile.id,
            operation="jd.parse_pdf",
            idempotency_key=idempotency_key,
            params={"raw_text": idempotency.body_digest(raw_text.encode("utf-8"))},
            perform=perform,
            read_back=lambda rid: read_parsed_jd(db, profile.id, rid),
            result_kind="job_description",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "jd.parse_pdf")


@router.get("/{jd_id}/requirements", response_model=list[RequirementRead])
def get_job_requirements(
    jd_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RequirementRead]:
    profile = get_or_create_profile(db, current_user)
    requirements = get_requirements(db, jd_id, profile.id)
    if requirements is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job description not found")
    return requirements


@router.post("/{jd_id}/match", response_model=JobMatchRead)
def match_job(
    jd_id: int,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobMatchRead:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[JobMatchRead, int]:
        try:
            result = match_resume_to_job(db, jd_id, profile.id)
        except NoResumeAnalysisError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Job description not found")
        return result, result.id

    try:
        # Deterministic, so a re-run would score the same -- but it inserts a JobMatch and a
        # fresh set of SkillGap rows each time, and those accumulate.
        return idempotency.run_once(
            db,
            profile.id,
            operation="jd.match",
            idempotency_key=idempotency_key,
            params={"jd_id": jd_id},
            perform=perform,
            read_back=lambda rid: read_match(db, profile.id, rid),
            result_kind="job_match",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "jd.match")


@router.post("/{jd_id}/preparation-plan", response_model=JobPreparationPlanRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
def create_preparation_plan(
    request: Request,
    jd_id: int,
    payload: GeneratePreparationPlanRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> JobPreparationPlanRead:
    profile = get_or_create_profile(db, current_user)
    # Deliberately no NoResumeAnalysisError handler: a student without a resume used to get a
    # 422 here and no plan at all, even though everything except the resume-match half is
    # derived from the job description alone. The service now returns a plan that names what it
    # could not assess (resume_recommendations.status == "no_resume") instead of refusing.
    # POST /{jd_id}/match above still 422s, because matching genuinely has nothing to match.
    def perform() -> tuple[JobPreparationPlanRead, int]:
        # ensure_preparation_plan, not generate_preparation_plan. The claim taken around this
        # call is keyed on the client's Idempotency-Key and so only ever sees one request's own
        # retransmissions; the business guard inside it is keyed on (profile, job description)
        # and is what stops two *different* requests from both paying for a model call to build
        # the one plan. Both layers are needed and neither replaces the other.
        result = ensure_preparation_plan(
            db, profile.id, jd_id, ai_provider, payload.company_name, payload.job_title
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Job description not found")
        return result, result.id

    try:
        # The client already reads before it writes here (GET first, POST only on a 404), which
        # covers the ordinary case. This covers the one it cannot see: the plan committed and
        # the reply was lost, where a replay would spend a second model pass on the same JD.
        return idempotency.run_once(
            db,
            profile.id,
            operation=PREPARATION_PLAN_OPERATION,
            idempotency_key=idempotency_key,
            params={
                "jd_id": jd_id,
                "company_name": payload.company_name,
                "job_title": payload.job_title,
            },
            perform=perform,
            read_back=lambda _rid: get_preparation_plan(db, jd_id, profile.id),
            result_kind="job_preparation_plan",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, PREPARATION_PLAN_OPERATION)


@router.get("/{jd_id}/preparation-plan", response_model=JobPreparationPlanRead)
def read_preparation_plan(
    jd_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobPreparationPlanRead:
    profile = get_or_create_profile(db, current_user)
    result = get_preparation_plan(db, jd_id, profile.id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No preparation plan found")
    return result


@router.delete("/{jd_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job_description_endpoint(
    jd_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    profile = get_or_create_profile(db, current_user)
    try:
        deleted = delete_job_description(db, jd_id, profile.id)
    except JobDescriptionInUseError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job description not found")
