import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.api.ai_errors import ai_unavailable
from app.api.deps import get_current_user, get_db
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
from app.services.jd_service import (
    JobDescriptionInUseError,
    delete_job_description,
    generate_preparation_plan,
    get_preparation_plan,
    get_requirements,
    list_job_descriptions,
    parse_job_description,
)
from app.services.matching_service import match_resume_to_job
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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> JobDescriptionParseResponse:
    profile = get_or_create_profile(db, current_user)
    try:
        return parse_job_description(db, profile.id, payload.raw_text, ai_provider)
    # JD parsing is one of the few AI paths with no template fallback in its service (there is
    # nothing sensible to invent from an unparsed job description), so AIGenerationError
    # reaches the route. Without this it escaped as an unhandled 500, which is the wrong
    # answer twice over: the client cannot distinguish "provider is down, retry" from "the
    # server is broken", and every provider blip logged a full stack trace for an expected
    # condition. Found by driving every AI-taking route with a failing provider; this was the
    # only one of the twenty still returning 500.
    except AIGenerationError as exc:
        raise ai_unavailable(exc, logger, "jobs")


@router.post("/upload", response_model=JobDescriptionParseResponse, status_code=status.HTTP_201_CREATED)
@limiter.shared_limit(JD_PARSE_LIMIT, scope=JD_PARSE_SCOPE)
async def create_job_description_from_pdf(
    request: Request,
    file: UploadFile = File(...),
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
    try:
        return await run_in_threadpool(
            parse_job_description, db, profile.id, raw_text, ai_provider, "pdf_upload"
        )
    except AIGenerationError as exc:
        raise ai_unavailable(exc, logger, "jobs")


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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobMatchRead:
    profile = get_or_create_profile(db, current_user)
    try:
        result = match_resume_to_job(db, jd_id, profile.id)
    except NoResumeAnalysisError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job description not found")
    return result


@router.post("/{jd_id}/preparation-plan", response_model=JobPreparationPlanRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
def create_preparation_plan(
    request: Request,
    jd_id: int,
    payload: GeneratePreparationPlanRequest,
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
    result = generate_preparation_plan(
        db, profile.id, jd_id, ai_provider, payload.company_name, payload.job_title
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job description not found")
    return result


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
