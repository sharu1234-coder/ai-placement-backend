"""Job Hunt API.

Two boundaries this module is responsible for.

*Provider errors never reach a student.* Every JobProviderError carries operator detail --
which credential was rejected, which host timed out -- and `detail=str(exc)` would put that in
a mobile app. So each failure class maps to its own status code and a fixed sentence, with the
real reason logged server-side against the request id. The status codes differ on purpose: the
client needs to tell "not set up yet" from "provider is down" from "you are searching too
fast", and it gets that from the code, not from parsed text. This is the same contract
app/api/ai_errors.py already established for AI failures.

*Ownership is resolved server-side.* Every handler derives profile_id from the authenticated
user through get_or_create_profile; no request body carries one. Rows owned by another student
answer 404, never 403, so an id cannot be confirmed by probing.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.job_listing import JobListing
from app.models.user import User
from app.schemas.job_hunt import (
    JobListingRead,
    JobMatchRead,
    JobResultRead,
    JobSearchProfileRead,
    JobSearchProfileUpdate,
    JobSearchRequest,
    JobSearchResponse,
    RoleSuggestionsRead,
    SavedJobRead,
    SavedSearchCreate,
    SavedSearchRead,
    SaveJobRequest,
    SkillProvenanceRead,
    UpdateSavedJobRequest,
)
from app.services.job_hunt import matching, service
from app.services.job_hunt import provenance as job_provenance
from app.services.job_hunt.providers.base import (
    JobProvider,
    JobProviderAuthError,
    JobProviderNotConfiguredError,
    JobProviderRateLimitError,
    JobProviderTimeoutError,
    JobProviderUnavailableError,
)
from app.services.job_hunt.providers.factory import get_job_provider
from app.services.profile_service import get_or_create_profile
from app.services.subscription_service import is_premium

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/job-hunt", tags=["job-hunt"])

# Filters the supplied product design marks as Premium ("Premium Features: Unlock advanced
# filters"). Deliberately limited to exactly what that design states: keywords, location and
# work mode stay free, so the free tier remains genuinely useful rather than a teaser. Any
# broader paywall is a business decision that has not been made and is not inferred here.
_PREMIUM_FILTER_FIELDS = ("salary_min", "employment_type", "experience_level", "max_days_old")


def _provider_failure(exc: Exception, context: str) -> HTTPException:
    """Map a provider failure to a status code plus a fixed, non-revealing sentence."""
    if isinstance(exc, JobProviderNotConfiguredError):
        # Logged at ERROR: this is a deployment gap only the owner can close, and it silently
        # disables a whole feature.
        logger.error("Job Hunt unavailable -- no provider configured (%s): %s", context, exc)
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job Hunt isn't available yet. Please check back soon.",
        )
    if isinstance(exc, JobProviderAuthError):
        # Also ERROR, and deliberately not a 401/403 to the student: nothing is wrong with
        # *their* credentials, and a revoked provider key must not look like a quiet job market.
        logger.error("Job Hunt provider rejected our credentials (%s): %s", context, exc)
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job search is temporarily unavailable. Please try again later.",
        )
    if isinstance(exc, JobProviderRateLimitError):
        logger.warning("Job Hunt provider rate limit hit (%s): %s", context, exc)
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many job searches right now. Please try again in a few minutes.",
        )
    if isinstance(exc, JobProviderTimeoutError):
        logger.warning("Job Hunt provider timed out (%s): %s", context, exc)
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="The job search took too long. Please try again.",
        )
    if isinstance(exc, JobProviderUnavailableError):
        logger.warning("Job Hunt provider unavailable (%s): %s", context, exc)
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The job service isn't responding right now. Please try again shortly.",
        )
    logger.exception("Unexpected Job Hunt provider failure (%s)", context)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="The job service isn't responding right now. Please try again shortly.",
    )


def _end_user_identity(request: Request) -> tuple[Optional[str], Optional[str]]:
    """The end user's IP and user agent, which Careerjet contractually requires on every query.

    Reads the socket peer, with the same deployment caveat documented at length in
    app/core/rate_limit.py: behind a reverse proxy this is the proxy's address unless Uvicorn
    is started with --proxy-headers and a real --forwarded-allow-ips. Providers that do not
    need this simply ignore it.
    """
    client_ip = request.client.host if request.client else None
    return client_ip, request.headers.get("user-agent")


# -- Job Hunt profile -----------------------------------------------------------------------


@router.get("/profile", response_model=JobSearchProfileRead)
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobSearchProfileRead:
    profile = get_or_create_profile(db, current_user)
    search_profile = service.get_or_create_search_profile(db, profile.id)
    return _to_profile_read(search_profile)


@router.put("/profile", response_model=JobSearchProfileRead)
def update_profile(
    payload: JobSearchProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobSearchProfileRead:
    profile = get_or_create_profile(db, current_user)
    search_profile = service.update_search_profile(
        db,
        profile.id,
        resume_id=payload.resume_id,
        target_role_id=payload.target_role_id,
        target_role_title=payload.target_role_title,
        declared_skills=payload.declared_skills,
        preferred_location=payload.preferred_location,
        work_mode=payload.work_mode,
        experience_level=payload.experience_level,
        employment_type=payload.employment_type,
        salary_min_expected=payload.salary_min_expected,
        salary_currency=payload.salary_currency,
        preferred_companies=payload.preferred_companies,
        open_to_relocation=payload.open_to_relocation,
        clear_resume=payload.clear_resume,
        clear_role=payload.clear_role,
    )
    return _to_profile_read(search_profile)


@router.get("/profile/role-suggestions", response_model=RoleSuggestionsRead)
def get_role_suggestions(
    resume_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RoleSuggestionsRead:
    """Conservative role guesses from a resume, for the student to confirm.

    Returns an empty list rather than 404 when the resume has no recognisable job titles --
    "we could not tell" is a normal outcome for a fresher's resume, not an error.
    """
    profile = get_or_create_profile(db, current_user)
    return RoleSuggestionsRead(
        suggested_role_families=service.suggest_roles_from_resume(db, profile.id, resume_id)
    )


def _to_profile_read(search_profile) -> JobSearchProfileRead:
    return JobSearchProfileRead(
        resume_id=search_profile.resume_id,
        target_role_id=search_profile.target_role_id,
        target_role_title=search_profile.target_role_title,
        declared_skills=list(search_profile.declared_skills or []),
        normalized_skills=list(search_profile.normalized_skills or []),
        preferred_location=search_profile.preferred_location,
        work_mode=search_profile.work_mode,
        experience_level=search_profile.experience_level,
        employment_type=search_profile.employment_type,
        salary_min_expected=search_profile.salary_min_expected,
        salary_currency=search_profile.salary_currency,
        preferred_companies=list(search_profile.preferred_companies or []),
        open_to_relocation=search_profile.open_to_relocation,
        signal_provenance=dict(search_profile.signal_provenance or {}),
        is_ready_to_search=bool(
            search_profile.target_role_title or search_profile.resume_id is not None
        ),
        updated_at=search_profile.updated_at,
    )


# -- Search ---------------------------------------------------------------------------------


@router.post("/search", response_model=JobSearchResponse)
# Deliberately strict. Every call spends the deployment's shared provider quota -- Adzuna's
# documented default is 25/minute and 250/day across the WHOLE deployment, not per student --
# so an unthrottled search endpoint would let a handful of users exhaust the day's budget for
# everyone. Revisit only alongside a provider plan that actually raises that ceiling.
@limiter.limit("20/hour")
def search_jobs(
    request: Request,
    payload: JobSearchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    provider: JobProvider = Depends(get_job_provider),
) -> JobSearchResponse:
    profile = get_or_create_profile(db, current_user)

    requested_premium_filters = [
        field for field in _PREMIUM_FILTER_FIELDS if getattr(payload, field) is not None
    ]
    if requested_premium_filters and not is_premium(db, profile.id):
        # Refused rather than silently ignored: quietly dropping a filter would show the
        # student unfiltered results while their UI claimed the filter was applied.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Advanced filters are a Premium feature. Search is free without them.",
        )

    end_user_ip, end_user_agent = _end_user_identity(request)

    try:
        outcome = service.search(
            db,
            profile,
            provider,
            keywords=payload.keywords,
            location=payload.location,
            work_mode=payload.work_mode,
            employment_type=payload.employment_type,
            experience_level=payload.experience_level,
            salary_min=payload.salary_min,
            max_days_old=payload.max_days_old,
            page=payload.page,
            page_size=payload.page_size,
            end_user_ip=end_user_ip,
            end_user_agent=end_user_agent,
        )
    except service.SearchProfileIncompleteError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except Exception as exc:
        raise _provider_failure(exc, "search")

    saved_rows = {
        saved.listing_id: saved
        for saved, _listing in service.list_saved_jobs(db, profile.id, limit=500)
    }

    search_profile = service.get_or_create_search_profile(db, profile.id)
    results = []
    for rank, listing in enumerate(outcome.listings):
        match = outcome.matches[listing.id]
        saved = saved_rows.get(listing.id)
        results.append(
            JobResultRead(
                listing=_to_listing_read(listing),
                match=_to_match_read(
                    match, listing, search_profile, outcome.evidence_states, rank=rank
                ),
                saved_job_id=saved.id if saved else None,
                saved_status=saved.status if saved else None,
            )
        )

    return JobSearchResponse(
        results=results,
        total_available=outcome.total_available,
        page=payload.page,
        page_size=payload.page_size,
        provider=outcome.provider_name,
        attribution_text=outcome.attribution_text,
        attribution_url=outcome.attribution_url,
        fetched_at=outcome.fetched_at,
        duplicates_removed=outcome.duplicates_removed,
        score_version=matching.SCORE_VERSION,
    )


@router.get("/jobs/{listing_id}", response_model=JobResultRead)
def get_job(
    listing_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobResultRead:
    profile = get_or_create_profile(db, current_user)
    try:
        listing, match = service.get_listing_for_profile(db, profile.id, listing_id)
    except service.ListingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    saved = next(
        (
            saved
            for saved, _listing in service.list_saved_jobs(db, profile.id, limit=500)
            if saved.listing_id == listing_id
        ),
        None,
    )
    # rank stays None: one job viewed on its own has no position in a ranked set, so it can
    # never be labelled "Best Match" off the back of a comparison that was never made.
    search_profile = service.get_or_create_search_profile(db, profile.id)
    evidence_states = service.evidence_states_for_skills(
        db,
        profile.id,
        list(match.matched_skills or []) + list(match.missing_skills or []),
    )
    return JobResultRead(
        listing=_to_listing_read(listing),
        match=_to_match_read(match, listing, search_profile, evidence_states),
        saved_job_id=saved.id if saved else None,
        saved_status=saved.status if saved else None,
    )


def _to_listing_read(listing) -> JobListingRead:
    return JobListingRead(
        id=listing.id,
        provider=listing.provider,
        url=listing.url,
        title=listing.title,
        company=listing.company,
        location=listing.location,
        work_mode=listing.work_mode,
        employment_type=listing.employment_type,
        seniority=listing.seniority,
        salary_min=listing.salary_min,
        salary_max=listing.salary_max,
        salary_currency=listing.salary_currency,
        salary_period=listing.salary_period,
        salary_is_predicted=listing.salary_is_predicted,
        description=listing.description,
        posted_at=listing.posted_at,
        fetched_at=listing.fetched_at,
        is_stale=service.is_stale(listing),
    )


def _to_match_read(
    match,
    listing,
    search_profile,
    evidence_states: dict,
    rank: Optional[int] = None,
) -> JobMatchRead:
    """`reasons`, `apply_readiness`, provenance and the ranking label are recomputed, not stored.

    The first two are presentation-layer derivations of the same component scores already
    persisted on the match row, and recomputing keeps a single source of truth: a stored copy
    could disagree with the stored components after a scoring change, and then the card and
    the breakdown would tell the student two different stories.

    Provenance is recomputed for a stronger reason -- it depends on evidence that changes
    underneath the stored match every time the student passes an assessment or finishes a
    project, so a stored copy would go wrong rather than merely stale.

    `search_profile` is passed in rather than looked up here: this runs once per listing, and
    fetching the same profile row twenty times per page was a real N+1 on the search path.
    """
    recomputed = matching.score_match(service.build_match_input(search_profile, listing))
    items = service.build_skill_provenance(search_profile, match, evidence_states)
    return JobMatchRead(
        total_score=match.total_score,
        score_version=match.score_version,
        component_scores=dict(match.component_scores or {}),
        matched_skills=list(match.matched_skills or []),
        missing_skills=list(match.missing_skills or []),
        unknown_factors=list(match.unknown_factors or []),
        rationale=match.rationale,
        reasons=recomputed.reasons,
        apply_readiness=recomputed.apply_readiness,
        skill_provenance=[
            SkillProvenanceRead(
                skill=item.skill,
                status=item.status,
                evidence_state=item.evidence_state,
                detail=item.detail,
            )
            for item in items
        ],
        verified_skill_count=job_provenance.verified_count(items),
        ranking_label=matching.ranking_label(
            rank=rank,
            total_score=match.total_score,
            component_count=len(match.component_scores or {}),
            apply_readiness=recomputed.apply_readiness,
        ),
    )


# -- Saved jobs / application tracker -------------------------------------------------------


@router.post("/saved", response_model=SavedJobRead, status_code=status.HTTP_201_CREATED)
def save_job(
    payload: SaveJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedJobRead:
    profile = get_or_create_profile(db, current_user)
    try:
        saved = service.save_job(db, profile.id, payload.listing_id, payload.status)
    except service.ListingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except service.InvalidStatusError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _to_saved_read(db, profile.id, saved)


@router.get("/saved", response_model=List[SavedJobRead])
def list_saved(
    status_filter: Optional[List[str]] = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[SavedJobRead]:
    profile = get_or_create_profile(db, current_user)
    rows = service.list_saved_jobs(db, profile.id, statuses=status_filter)
    matches = service.get_saved_job_matches(db, profile.id, [listing.id for _s, listing in rows])
    return [
        SavedJobRead(
            id=saved.id,
            status=saved.status,
            notes=saved.notes,
            follow_up_date=saved.follow_up_date,
            applied_at=saved.applied_at,
            created_at=saved.created_at,
            updated_at=saved.updated_at,
            listing=_to_listing_read(listing),
            match=_stored_match_read(matches.get(listing.id)),
        )
        for saved, listing in rows
    ]


@router.patch("/saved/{saved_job_id}", response_model=SavedJobRead)
def update_saved(
    saved_job_id: int,
    payload: UpdateSavedJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedJobRead:
    profile = get_or_create_profile(db, current_user)
    try:
        saved = service.update_saved_job(
            db,
            profile.id,
            saved_job_id,
            status=payload.status,
            notes=payload.notes,
            follow_up_date=payload.follow_up_date,
        )
    except service.SavedJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except service.InvalidStatusError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _to_saved_read(db, profile.id, saved)


@router.delete("/saved/{saved_job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved(
    saved_job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    profile = get_or_create_profile(db, current_user)
    try:
        service.delete_saved_job(db, profile.id, saved_job_id)
    except service.SavedJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _to_saved_read(db: Session, profile_id: int, saved) -> SavedJobRead:
    listing = db.get(JobListing, saved.listing_id)
    matches = service.get_saved_job_matches(db, profile_id, [saved.listing_id])
    return SavedJobRead(
        id=saved.id,
        status=saved.status,
        notes=saved.notes,
        follow_up_date=saved.follow_up_date,
        applied_at=saved.applied_at,
        created_at=saved.created_at,
        updated_at=saved.updated_at,
        listing=_to_listing_read(listing),
        match=_stored_match_read(matches.get(saved.listing_id)),
    )


def _stored_match_read(match) -> Optional[JobMatchRead]:
    """Reads a persisted match without recomputing `reasons`.

    The tracker lists many rows; recomputing every one would re-derive the whole engine per
    row. `reasons` is empty here rather than fabricated, and the detail screen (which does
    recompute) is where the full explanation lives.
    """
    if match is None:
        return None
    return JobMatchRead(
        total_score=match.total_score,
        score_version=match.score_version,
        component_scores=dict(match.component_scores or {}),
        matched_skills=list(match.matched_skills or []),
        missing_skills=list(match.missing_skills or []),
        unknown_factors=list(match.unknown_factors or []),
        rationale=match.rationale,
        reasons=[],
        apply_readiness=None,
    )


# -- Saved searches -------------------------------------------------------------------------


@router.post("/saved-searches", response_model=SavedSearchRead, status_code=status.HTTP_201_CREATED)
def create_saved_search(
    payload: SavedSearchCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedSearchRead:
    profile = get_or_create_profile(db, current_user)
    saved_search = service.create_saved_search(
        db,
        profile.id,
        name=payload.name,
        filters=payload.filters.model_dump(exclude_none=True),
        notify_enabled=payload.notify_enabled,
    )
    return SavedSearchRead.model_validate(saved_search)


@router.get("/saved-searches", response_model=List[SavedSearchRead])
def list_saved_searches(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[SavedSearchRead]:
    profile = get_or_create_profile(db, current_user)
    return [
        SavedSearchRead.model_validate(row) for row in service.list_saved_searches(db, profile.id)
    ]


@router.delete("/saved-searches/{saved_search_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_search(
    saved_search_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    profile = get_or_create_profile(db, current_user)
    try:
        service.delete_saved_search(db, profile.id, saved_search_id)
    except service.SavedJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
