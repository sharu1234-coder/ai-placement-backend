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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.core.rate_limit import limiter
from app.models.job_alert import JobAlert
from app.models.job_listing import JobListing
from app.models.job_saved_search import JobSavedSearch
from app.models.saved_job import SavedJob
from app.models.user import User
from app.schemas.job_hunt import (
    ApplicationReadinessRead,
    JobAlertCreate,
    JobAlertHistoryRead,
    JobAlertQuotaRead,
    JobAlertRead,
    JobAlertUpdate,
    JobHuntEntitlementRead,
    JobHuntHomeRead,
    JobHuntLimitDetail,
    JobListingRead,
    JobMatchRead,
    JobResultRead,
    JobSearchProfileRead,
    JobSearchProfileUpdate,
    JobSearchRequest,
    JobSearchResponse,
    ManualJobCreate,
    ReadinessDimensionRead,
    ReadinessGapRead,
    RoleSuggestionsRead,
    SavedJobRead,
    SavedJobStatus,
    SavedSearchCreate,
    SavedSearchRead,
    SaveJobRequest,
    SkillMatchAnalysisRead,
    SkillMatchRowRead,
    SkillProvenanceRead,
    UpdateSavedJobRequest,
)
from app.services import idempotency
from app.services.job_hunt import alerts as job_alerts
from app.services.job_hunt import entitlement, matching, readiness, service
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


def _refuse_premium_filters(db: Session, profile_id: int, values: dict) -> None:
    """Raise 403 if `values` uses an advanced filter this student is not entitled to.

    Applied at every point a filter set can reach the provider, not just POST /search.
    Saved searches store a filter set and replay it, so checking only the direct search path
    left the paywall bypassable in two ordinary requests: store the filters once, then run the
    saved search as often as you like.

    Checked against the plan at the moment of use rather than at the moment of storing, so a
    subscription that lapses stops working immediately instead of grandfathering premium
    filtering in forever.
    """
    requested = [
        field
        for field in _PREMIUM_FILTER_FIELDS
        if values.get(field) is not None
    ]
    if requested and not is_premium(db, profile_id):
        # Refused rather than silently ignored: quietly dropping a filter would show the
        # student unfiltered results while their UI claimed the filter was applied.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Advanced filters are a Premium feature. Search is free without them.",
        )


def _claim_job_hunt_use(
    db: Session,
    profile_id: int,
    *,
    usage_type: str,
    idempotency_key: Optional[str],
    params: dict,
):
    """Reserve this student's right to run one Job Hunt operation, or refuse in HTTP terms.

    Three refusals, three status codes, because the client has to do three different things.

    402 Payment Required is the free-limit refusal. Not 403: this endpoint already answers 403
    for "you used a Premium-only filter", and a client that had to tell those apart by reading
    the message text would be one copy edit away from opening the wrong screen. 402 is
    unambiguous, and it carries a structured body (see JobHuntLimitDetail) so the upgrade
    prompt is driven by a stable `code` rather than by prose.

    409 Conflict is an idempotency key that names a different operation -- a different search
    under a key already spent, or a key replayed long after its operation finished. Both are
    "this is not the request you say it is", and neither is a reason to quietly charge the
    student for a search they did not ask for.

    400 is a malformed key. The client sends a UUID; anything else is a bug on its side, and
    inventing a substitute would mean silently not deduplicating a request that expected to be.
    """
    try:
        key = entitlement.normalise_key(idempotency_key)
    except entitlement.InvalidIdempotencyKeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        return entitlement.claim(
            db,
            profile_id,
            usage_type=usage_type,
            idempotency_key=key,
            request_fingerprint=entitlement.fingerprint(usage_type, params),
        )
    except entitlement.FreeLimitReachedError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=JobHuntLimitDetail(message=str(exc)).model_dump(),
        )
    except entitlement.IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


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
    # Optional. When Android sends one, a retry of the same tap -- after a dropped connection,
    # a recreated activity, a killed process -- is recognised as the same operation and does
    # not cost a second search. It can never do the opposite: see entitlement.normalise_key.
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JobSearchResponse:
    profile = get_or_create_profile(db, current_user)

    # Before the claim, deliberately. A search refused for using a Premium-only filter never
    # happened, and charging a free student their one search for a request the server declined
    # to run would be indefensible.
    _refuse_premium_filters(db, profile.id, payload.model_dump())

    usage = _claim_job_hunt_use(
        db,
        profile.id,
        usage_type="search",
        idempotency_key=idempotency_key,
        params=payload.model_dump(mode="json"),
    )

    end_user_ip, end_user_agent = _end_user_identity(request)

    # Everything from here to the settle() is inside the claim. Whatever happens -- a provider
    # outage, an incomplete profile, an unexpected failure while assembling the response -- the
    # allowance goes back unless a real result was produced. The one exception is a claim that
    # had already succeeded on an earlier attempt with this key: release() only ever moves a
    # row out of "claimed", so a replayed request that fails cannot refund a search that
    # genuinely worked.
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
            sort=payload.sort,
            end_user_ip=end_user_ip,
            end_user_agent=end_user_agent,
        )

        saved_rows = service.saved_jobs_for_listings(
            db, profile.id, [listing.id for listing in outcome.listings]
        )

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

        response = JobSearchResponse(
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
    except service.SearchProfileIncompleteError as exc:
        entitlement.release(db, usage)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except HTTPException:
        entitlement.release(db, usage)
        raise
    except Exception as exc:
        entitlement.release(db, usage)
        raise _provider_failure(exc, "search")

    # A real result set, from a real provider call. This is the moment the free search is
    # spent -- not when the screen was opened, not when filters were edited, and not once per
    # provider request, page or job inside the operation that produced it.
    entitlement.settle(db, usage)
    return response


@router.get("/entitlement", response_model=JobHuntEntitlementRead)
def job_hunt_entitlement(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobHuntEntitlementRead:
    """Whether this student may run a search, so the client can say so before they tap.

    Purely so the screen can be honest up front -- "try Job Hunt once, free" before the search,
    an upgrade prompt after -- rather than presenting a button that turns out to be refused.
    Reading this grants nothing and consumes nothing; POST /search re-derives every one of
    these facts for itself, so a client that skips this call, caches a stale copy of it, or
    edits the response in memory is in exactly the same position either way.
    """
    profile = get_or_create_profile(db, current_user)
    return JobHuntEntitlementRead(**entitlement.status_for(db, profile.id))


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

    saved = service.saved_jobs_for_listings(db, profile.id, [listing_id]).get(listing_id)
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
        preferred_company_match=recomputed.preferred_company_match,
        ranking_label=matching.ranking_label(
            rank=rank,
            total_score=match.total_score,
            component_count=len(match.component_scores or {}),
            apply_readiness=recomputed.apply_readiness,
        ),
    )


# -- Saved jobs / application tracker -------------------------------------------------------


@router.post("/listings", response_model=SavedJobRead, status_code=status.HTTP_201_CREATED)
def create_manual_listing(
    payload: ManualJobCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedJobRead:
    """Add a job the student found outside this app, and start tracking it.

    The one creation path for a listing that did not come from a job provider. Everything
    downstream -- the tracker, the Career Goal link, the stage pipeline, the outcome, Home --
    reads a SavedJob and cannot tell the difference, which is the whole point: where a student
    found a job should not decide whether the product can help them pursue it.

    Deterministic end to end. No provider is called, so this costs no quota and works with
    JOB_PROVIDER unset entirely; no AI is called, so a pasted job description is stored, not
    interpreted. The URL is validated and stored, never fetched.

    Returns the SavedJobRead rather than a bare listing, because a listing nobody is tracking
    is not something a student can do anything with -- and because it gives the client the
    tracker row it needs next in the same round trip.
    """
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[SavedJobRead, int]:
        saved = _add_manual(db, profile.id, payload)
        return _to_saved_read(db, profile.id, saved), saved.id

    def read_back(saved_job_id: int) -> Optional[SavedJobRead]:
        existing = db.get(SavedJob, saved_job_id)
        if existing is None or existing.profile_id != profile.id:
            return None
        return _to_saved_read(db, profile.id, existing)

    try:
        # add_manual_job writes a JobListing and the SavedJob that tracks it, so a replay puts
        # the same job in the student's tracker twice.
        return idempotency.run_once(
            db,
            profile.id,
            operation="job_hunt.manual_listing",
            idempotency_key=idempotency_key,
            params={"title": payload.title, "company": payload.company, "url": payload.url,
                    "location": payload.location, "status": payload.status},
            perform=perform,
            read_back=read_back,
            result_kind="saved_job",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "job_hunt.manual_listing")


def _add_manual(db: Session, profile_id: int, payload: ManualJobCreate):
    """The original create, unchanged, lifted out so the guard can wrap it."""
    try:
        saved = service.add_manual_job(
            db,
            profile_id,
            title=payload.title,
            company=payload.company,
            location=payload.location,
            url=payload.url,
            description=payload.description,
            status=payload.status,
        )
    except service.InvalidJobUrlError as exc:
        # 422 with the message, unlike the provider errors above: this one is about what the
        # student typed, so it is the one class of failure they can actually act on.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except service.InvalidStatusError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return saved


@router.post("/saved", response_model=SavedJobRead, status_code=status.HTTP_201_CREATED)
def save_job(
    payload: SaveJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedJobRead:
    profile = get_or_create_profile(db, current_user)
    try:
        saved = service.save_job(
            db,
            profile.id,
            payload.listing_id,
            payload.status,
            source=payload.source,
            source_saved_search_id=payload.source_saved_search_id,
        )
    except (service.ListingNotFoundError, service.SavedSearchNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except service.InvalidStatusError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _to_saved_read(db, profile.id, saved)


@router.get("/saved", response_model=List[SavedJobRead])
def list_saved(
    # Typed rather than a bare str: an unrecognised status used to filter to nothing and
    # render as "you have no applications", which reads as data loss rather than as a bad
    # request. It is now a 422 that names the valid values.
    status_filter: Optional[List[SavedJobStatus]] = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[SavedJobRead]:
    profile = get_or_create_profile(db, current_user)
    rows = service.list_saved_jobs(db, profile.id, statuses=status_filter)
    matches = service.get_saved_job_matches(db, profile.id, [listing.id for _s, listing in rows])
    # Saved-search names resolved in one query rather than one per row, and only for the rows
    # that actually carry a provenance id.
    wanted = {
        saved.source_saved_search_id
        for saved, _listing in rows
        if saved.source_saved_search_id is not None
    }
    # Scoped to this student as well as to the wanted ids. Defence in depth behind
    # service.save_job's ownership check, and the part that protects rows written before that
    # check existed: a foreign id already sitting in the column must still not resolve to a
    # name here.
    search_names = (
        {
            row.id: row.name
            for row in db.query(JobSavedSearch)
            .filter(
                JobSavedSearch.id.in_(wanted),
                JobSavedSearch.profile_id == profile.id,
            )
            .all()
        }
        if wanted
        else {}
    )
    return [
        SavedJobRead(
            id=saved.id,
            status=saved.status,
            notes=saved.notes,
            follow_up_date=saved.follow_up_date,
            applied_at=saved.applied_at,
            created_at=saved.created_at,
            updated_at=saved.updated_at,
            source=saved.source,
            source_saved_search_id=saved.source_saved_search_id,
            source_saved_search_name=search_names.get(saved.source_saved_search_id),
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
        source=saved.source,
        source_saved_search_id=saved.source_saved_search_id,
        # Resolved for display. Null when the saved search has since been deleted -- the row
        # keeps its provenance rather than being rewritten to "manual", because the student
        # really did find this job through a search, and a tracker that edits its own history
        # to match the present is worse than one that admits the search is gone.
        # Looked up by (id, profile_id), never by primary key alone -- see the note on the
        # batch lookup in list_saved.
        source_saved_search_name=(
            search.name
            if saved.source_saved_search_id
            and (
                search := db.query(JobSavedSearch)
                .filter(
                    JobSavedSearch.id == saved.source_saved_search_id,
                    JobSavedSearch.profile_id == profile_id,
                )
                .first()
            )
            else None
        ),
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
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedSearchRead:
    profile = get_or_create_profile(db, current_user)
    filters = payload.filters.model_dump(exclude_none=True)
    # Refused at the point of storing as well as at the point of running, so a free student is
    # told now rather than handed a saved search that fails every time they tap it.
    _refuse_premium_filters(db, profile.id, filters)

    def perform() -> tuple[SavedSearchRead, int]:
        saved_search = service.create_saved_search(
            db,
            profile.id,
            name=payload.name,
            filters=filters,
            notify_enabled=payload.notify_enabled,
        )
        return SavedSearchRead.model_validate(saved_search), saved_search.id

    def read_back(search_id: int) -> Optional[SavedSearchRead]:
        existing = db.get(JobSavedSearch, search_id)
        if existing is None or existing.profile_id != profile.id:
            return None
        return SavedSearchRead.model_validate(existing)

    try:
        # Nothing deduplicates saved searches, so a replay leaves the student with the same
        # search listed twice -- visible on their own screen and theirs to delete.
        return idempotency.run_once(
            db,
            profile.id,
            operation="job_hunt.save_search",
            idempotency_key=idempotency_key,
            params={"name": payload.name, "filters": filters,
                    "notify_enabled": payload.notify_enabled},
            perform=perform,
            read_back=read_back,
            result_kind="job_saved_search",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "job_hunt.save_search")


@router.get("/saved-searches", response_model=List[SavedSearchRead])
def list_saved_searches(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[SavedSearchRead]:
    profile = get_or_create_profile(db, current_user)
    rows = service.list_saved_searches(db, profile.id)
    # One query for every alert, rather than one per search. The list is small, but the shape
    # of the loop is what would make it an N+1 as soon as it is not.
    alerted = {
        alert.saved_search_id
        for alert in job_alerts.list_alerts(db, profile.id)
        if alert.saved_search_id is not None
    }
    out = []
    for row in rows:
        read = SavedSearchRead.model_validate(row)
        out.append(read.model_copy(update={"has_alert": row.id in alerted}))
    return out


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


# -- Home -----------------------------------------------------------------------------------


@router.get("/home", response_model=JobHuntHomeRead)
def job_hunt_home(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobHuntHomeRead:
    """Counts and top recommendations, from what we have already fetched and scored.

    Deliberately does NOT call the provider. Opening the app should not consume a quota unit
    or take a network round trip's worth of latency before showing anything, and the counts
    describe this student's own stored rows rather than the live market.
    """
    profile = get_or_create_profile(db, current_user)
    search_profile = service.get_or_create_search_profile(db, profile.id)
    counts = service.home_summary(db, profile.id)

    listings, matches = service.top_matches(db, profile.id, limit=5)
    evidence_states = service.evidence_states_for_skills(
        db,
        profile.id,
        [
            skill
            for match in matches.values()
            for skill in list(match.matched_skills or []) + list(match.missing_skills or [])
        ],
    )
    saved_rows = service.saved_jobs_for_listings(
        db, profile.id, [listing.id for listing in listings]
    )
    recommended = [
        JobResultRead(
            listing=_to_listing_read(listing),
            match=_to_match_read(
                matches[listing.id], listing, search_profile, evidence_states, rank=rank
            ),
            saved_job_id=saved_rows[listing.id].id if listing.id in saved_rows else None,
            saved_status=saved_rows[listing.id].status if listing.id in saved_rows else None,
        )
        for rank, listing in enumerate(listings)
    ]

    return JobHuntHomeRead(
        new_jobs=counts["new_jobs"],
        best_matches=counts["best_matches"],
        applied=counts["applied"],
        saved=counts["saved"],
        target_role_title=search_profile.target_role_title,
        is_ready_to_search=bool(
            search_profile.target_role_title or search_profile.resume_id is not None
        ),
        recommended=recommended,
    )


# -- Skill match analysis + application readiness --------------------------------------------


@router.get("/jobs/{listing_id}/skill-analysis", response_model=SkillMatchAnalysisRead)
def job_skill_analysis(
    listing_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SkillMatchAnalysisRead:
    profile = get_or_create_profile(db, current_user)
    try:
        listing, match = service.get_listing_for_profile(db, profile.id, listing_id)
    except service.ListingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    search_profile = service.get_or_create_search_profile(db, profile.id)
    evidence_states = service.evidence_states_for_skills(
        db, profile.id, list(match.matched_skills or []) + list(match.missing_skills or [])
    )
    items = service.build_skill_provenance(search_profile, match, evidence_states)

    gaps = [item.skill for item in items if item.status == job_provenance.MISSING]
    return SkillMatchAnalysisRead(
        total_score=match.total_score,
        score_version=match.score_version,
        rows=[
            SkillMatchRowRead(
                skill=item.skill,
                status=item.status,
                strength=job_provenance.match_strength(item),
                evidence_state=item.evidence_state,
                detail=item.detail,
            )
            for item in items
        ],
        verified_skill_count=job_provenance.verified_count(items),
        # The first real gap, or nothing. Never a filler value to keep the card populated.
        biggest_gap_skill=gaps[0] if gaps else None,
    )


@router.get("/jobs/{listing_id}/readiness", response_model=ApplicationReadinessRead)
def job_application_readiness(
    listing_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApplicationReadinessRead:
    """Premium: the job-specific readiness breakdown.

    Free students still get the match score, the four skill buckets and the gap list -- the
    diagnosis is not paywalled. What Premium adds is the per-dimension breakdown and the
    before-you-apply plan.
    """
    profile = get_or_create_profile(db, current_user)
    if not is_premium(db, profile.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Application readiness is a Premium feature. Your match score and skill gaps "
                "stay free."
            ),
        )

    try:
        listing, match = service.get_listing_for_profile(db, profile.id, listing_id)
    except service.ListingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    search_profile = service.get_or_create_search_profile(db, profile.id)
    evidence_states = service.evidence_states_for_skills(
        db, profile.id, list(match.matched_skills or []) + list(match.missing_skills or [])
    )
    items = service.build_skill_provenance(search_profile, match, evidence_states)
    result = readiness.compute(
        db, profile, listing, dict(match.component_scores or {}), items
    )

    return ApplicationReadinessRead(
        listing_id=listing.id,
        overall=result.overall,
        band=result.band,
        dimensions=[
            ReadinessDimensionRead(
                key=dim.key, label=dim.label, score=dim.score, detail=dim.detail
            )
            for dim in result.dimensions
        ],
        unknown_dimensions=result.unknown_dimensions,
        biggest_gap=ReadinessGapRead(**result.biggest_gap) if result.biggest_gap else None,
        tips=result.tips,
    )


# -- Saved-search execution ------------------------------------------------------------------


@router.post("/saved-searches/{saved_search_id}/run", response_model=JobSearchResponse)
@limiter.limit("20/hour")
def run_saved_search(
    request: Request,
    saved_search_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    provider: JobProvider = Depends(get_job_provider),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JobSearchResponse:
    """Re-run a stored search, and record that it ran.

    Spends the same allowance as POST /search, for the same reason it shares that endpoint's
    rate limit: a saved search *is* a search. Giving it its own budget would turn "save this,
    then run it" into an unlimited free tier reached in two ordinary requests -- the same
    paywall bypass that already had to be closed for advanced filters, one endpoint over.

    Storing a saved search is free, and so is listing or deleting one. Only running it reaches
    a provider, and only reaching a provider costs anything.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        stored = service.get_saved_search(db, profile.id, saved_search_id)
    except service.SavedSearchNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    # Against the stored filter set, before any provider call is made.
    _refuse_premium_filters(db, profile.id, dict(stored.filters or {}))

    usage = _claim_job_hunt_use(
        db,
        profile.id,
        usage_type="saved_search_run",
        idempotency_key=idempotency_key,
        # The saved search's id is part of the identity of the operation: running search A and
        # running search B are two different things even in the rare case their stored filters
        # happen to match.
        params={"saved_search_id": saved_search_id, **dict(stored.filters or {})},
    )

    end_user_ip, end_user_agent = _end_user_identity(request)

    try:
        saved_search, outcome = service.run_saved_search(
            db,
            profile,
            provider,
            saved_search_id,
            end_user_ip=end_user_ip,
            end_user_agent=end_user_agent,
        )

        search_profile = service.get_or_create_search_profile(db, profile.id)
        saved_rows = service.saved_jobs_for_listings(
            db, profile.id, [listing.id for listing in outcome.listings]
        )
        results = [
            JobResultRead(
                listing=_to_listing_read(listing),
                match=_to_match_read(
                    outcome.matches[listing.id],
                    listing,
                    search_profile,
                    outcome.evidence_states,
                    rank=rank,
                ),
                saved_job_id=saved_rows[listing.id].id if listing.id in saved_rows else None,
                saved_status=saved_rows[listing.id].status if listing.id in saved_rows else None,
            )
            for rank, listing in enumerate(outcome.listings)
        ]
        filters = dict(saved_search.filters or {})
        response = JobSearchResponse(
            results=results,
            total_available=outcome.total_available,
            page=filters.get("page") or 1,
            page_size=filters.get("page_size") or 20,
            provider=outcome.provider_name,
            attribution_text=outcome.attribution_text,
            attribution_url=outcome.attribution_url,
            fetched_at=outcome.fetched_at,
            duplicates_removed=outcome.duplicates_removed,
            score_version=matching.SCORE_VERSION,
        )
    except service.SavedSearchNotFoundError as exc:
        entitlement.release(db, usage)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except service.SearchProfileIncompleteError as exc:
        entitlement.release(db, usage)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except HTTPException:
        entitlement.release(db, usage)
        raise
    except Exception as exc:
        entitlement.release(db, usage)
        raise _provider_failure(exc, "saved search run")

    entitlement.settle(db, usage)
    return response


# -- Job alerts ------------------------------------------------------------------------------


def _to_alert_read(db: Session, alert, searches: Optional[dict] = None) -> JobAlertRead:
    """`searches` lets a list caller prefetch every saved search in one query. Without it this
    falls back to a single lookup, which is right for the create/update paths that hold one
    alert and wrong for the list path that holds ten."""
    if alert.saved_search_id is None:
        search = None
    elif searches is not None:
        search = searches.get(alert.saved_search_id)
    else:
        # Scoped to the alert's own profile rather than fetched by primary key. create_alert
        # already refuses a saved search the student does not own, so this cannot currently
        # resolve a foreign row -- but keeping the check local means the guarantee does not
        # depend on a validation three files away staying correct.
        search = (
            db.query(JobSavedSearch)
            .filter(
                JobSavedSearch.id == alert.saved_search_id,
                JobSavedSearch.profile_id == alert.profile_id,
            )
            .first()
        )
    # Filters are read live from the saved search when there is one, matching what the alert
    # will actually run with -- see alerts._filters_for. Unknown keys are dropped rather than
    # passed through: stored filters are data, and data written by an older version of the app
    # must not be able to reach a field that did not exist when it was saved.
    raw_filters = dict((search.filters if search else alert.filters) or {})
    return JobAlertRead(
        id=alert.id,
        name=alert.name,
        saved_search_id=alert.saved_search_id,
        saved_search_name=search.name if search else None,
        filters=JobSearchRequest(
            **{
                key: value
                for key, value in raw_filters.items()
                if key in JobSearchRequest.model_fields
            }
        ),
        frequency=alert.frequency,
        day_of_week=alert.day_of_week,
        hour_local=alert.hour_local,
        quiet_hours_start=alert.quiet_hours_start,
        quiet_hours_end=alert.quiet_hours_end,
        is_enabled=alert.is_enabled,
        last_run_at=alert.last_run_at,
        last_success_at=alert.last_success_at,
        last_error_at=alert.last_error_at,
        last_run_status=alert.last_run_status,
        last_new_job_count=alert.last_new_job_count,
        created_at=alert.created_at,
    )


@router.get("/alerts", response_model=List[JobAlertRead])
def list_job_alerts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[JobAlertRead]:
    profile = get_or_create_profile(db, current_user)
    alerts = job_alerts.list_alerts(db, profile.id)
    wanted = {alert.saved_search_id for alert in alerts if alert.saved_search_id is not None}
    searches = (
        {
            row.id: row
            for row in db.query(JobSavedSearch)
            .filter(
                JobSavedSearch.id.in_(wanted),
                JobSavedSearch.profile_id == profile.id,
            )
            .all()
        }
        if wanted
        else {}
    )
    return [_to_alert_read(db, alert, searches) for alert in alerts]


@router.get("/alerts/quota", response_model=JobAlertQuotaRead)
def job_alert_quota(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobAlertQuotaRead:
    """What the plan allows. Returned so the client can explain a refusal rather than showing
    a disabled button with no reason attached."""
    profile = get_or_create_profile(db, current_user)
    return JobAlertQuotaRead(
        used=len(job_alerts.list_alerts(db, profile.id)),
        limit=job_alerts.max_alerts_for(db, profile.id),
        is_premium=is_premium(db, profile.id),
    )


@router.get("/alerts/history", response_model=List[JobAlertHistoryRead])
def job_alert_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[JobAlertHistoryRead]:
    profile = get_or_create_profile(db, current_user)
    rows = job_alerts.list_history(db, profile.id)
    names = {alert.id: alert.name for alert in job_alerts.list_alerts(db, profile.id)}
    # One query for every listing in the history rather than one per row. db.get looks like a
    # cheap identity-map read, but on a first pass over fifty deliveries it is fifty selects.
    listings = {
        listing.id: listing
        for listing in db.query(JobListing)
        .filter(JobListing.id.in_({row.listing_id for row in rows}))
        .all()
    } if rows else {}
    out: List[JobAlertHistoryRead] = []
    for row in rows:
        listing = listings.get(row.listing_id)
        if listing is None:
            # The shared listing was pruned. The delivery still happened, but there is nothing
            # to show, so the row is skipped rather than rendered as a blank card.
            continue
        out.append(
            JobAlertHistoryRead(
                id=row.id,
                alert_id=row.alert_id,
                alert_name=names.get(row.alert_id),
                listing=_to_listing_read(listing),
                notified_at=row.notified_at,
            )
        )
    return out


@router.post("/alerts", response_model=JobAlertRead, status_code=status.HTTP_201_CREATED)
def create_job_alert(
    payload: JobAlertCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobAlertRead:
    profile = get_or_create_profile(db, current_user)
    # A standalone alert carries its own filter set and run_alert replays it, so it is a third
    # way a filter set reaches the provider and is gated like the other two. (An alert bound to
    # a saved search reads that search's filters instead, and those were gated when the search
    # was stored.)
    if payload.filters is not None:
        _refuse_premium_filters(db, profile.id, payload.filters.model_dump(exclude_none=True))

    def perform() -> tuple[JobAlertRead, int]:
        try:
            alert = job_alerts.create_alert(
                db,
                profile.id,
                name=payload.name,
                saved_search_id=payload.saved_search_id,
                filters=payload.filters.model_dump(exclude_none=True) if payload.filters else None,
                frequency=payload.frequency,
                day_of_week=payload.day_of_week,
                hour_local=payload.hour_local,
                quiet_hours_start=payload.quiet_hours_start,
                quiet_hours_end=payload.quiet_hours_end,
                is_enabled=payload.is_enabled,
            )
        except job_alerts.AlertNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        except job_alerts.AlertLimitReachedError as exc:
            # 403 and not 409: this is an entitlement ceiling, and the client routes it to the
            # upgrade prompt rather than to a retry.
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
        except job_alerts.InvalidAlertError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        return _to_alert_read(db, alert), alert.id

    def read_back(alert_id: int) -> Optional[JobAlertRead]:
        existing = db.get(JobAlert, alert_id)
        if existing is None or existing.profile_id != profile.id:
            return None
        return _to_alert_read(db, existing)

    try:
        # create_alert already returns the existing alert for a repeat on the same saved
        # search, but the client's savedSearchId defaults to null and *that* path inserts
        # unconditionally -- so a replay of a filters-only alert means duplicate notifications.
        return idempotency.run_once(
            db,
            profile.id,
            operation="job_hunt.create_alert",
            idempotency_key=idempotency_key,
            params={
                "name": payload.name,
                "saved_search_id": payload.saved_search_id,
                "filters": payload.filters.model_dump(exclude_none=True) if payload.filters else None,
                "frequency": payload.frequency,
            },
            perform=perform,
            read_back=read_back,
            result_kind="job_alert",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "job_hunt.create_alert")


@router.patch("/alerts/{alert_id}", response_model=JobAlertRead)
def update_job_alert(
    alert_id: int,
    payload: JobAlertUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobAlertRead:
    profile = get_or_create_profile(db, current_user)
    try:
        alert = job_alerts.update_alert(
            db,
            profile.id,
            alert_id,
            name=payload.name,
            frequency=payload.frequency,
            day_of_week=payload.day_of_week,
            hour_local=payload.hour_local,
            quiet_hours_start=payload.quiet_hours_start,
            quiet_hours_end=payload.quiet_hours_end,
            is_enabled=payload.is_enabled,
            clear_quiet_hours=payload.clear_quiet_hours,
        )
    except job_alerts.AlertNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except job_alerts.InvalidAlertError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _to_alert_read(db, alert)


@router.delete("/alerts/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job_alert(
    alert_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    profile = get_or_create_profile(db, current_user)
    try:
        job_alerts.delete_alert(db, profile.id, alert_id)
    except job_alerts.AlertNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
