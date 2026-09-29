"""Job Hunt orchestration: profile, search, matching, saving, application tracking.

Ownership rule, applied everywhere in this module: every query is filtered by the profile_id
resolved server-side from the authenticated user (via get_or_create_profile), never by an id
supplied in a request body. A listing is the one shared object -- provider content many
students can see -- so anything *about* a student's relationship to a listing (their match,
their saved row) is keyed to their profile and looked up through it.

Provider failures are never converted into empty results. `search` raises, and the API layer
turns each failure class into its own response, because "we could not ask" and "there is
nothing" are different facts and a student deserves to know which one they are looking at.
"""

import datetime
import hashlib
import logging
import uuid
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.job_alert import JobAlert, JobAlertDelivery
from app.models.job_listing import JobListing
from app.models.job_listing_match import JobListingMatch
from app.models.job_saved_search import JobSavedSearch
from app.models.job_search_profile import JobSearchProfile
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.role import Role
from app.models.saved_job import SAVED_JOB_SOURCES, SAVED_JOB_STATUSES, SavedJob
from app.services import evidence_engine
from app.services.job_hunt import dedupe, matching, normalization, provenance, provider_budget
from app.services.job_hunt.providers.base import (
    JobProvider,
    ProviderJob,
    ProviderSearchQuery,
)

logger = logging.getLogger(__name__)

# Applied to a single search request. Above this the provider quota (Adzuna's documented
# default is 25 hits/minute) is burned by one student.
MAX_PAGE_SIZE = 50

# Clamped here as well as in the request schema, for the same reason MAX_PAGE_SIZE is: the
# schema only guards the HTTP path, and search() is also reached from a stored filter set (a
# saved search, an alert). Careerjet refuses pages past 10 outright and Adzuna would simply
# serve an empty page, so an unclamped value turns into a silent no-results rather than an
# error.
MAX_PAGE = 10

# How recently a listing must have been fetched to count as "new" on the home screen. A week,
# because that is roughly how often a student in an active search comes back, and a shorter
# window would show zero to anyone who took a weekend off.
NEW_JOB_WINDOW_DAYS = 7

# The bar for the "best matches" tile. Deliberately the same constant the ranking labels use,
# imported rather than re-declared, so the tile count and the "Strong fit" badges on the cards
# can never tell the student two different stories.
STRONG_FIT_THRESHOLD = matching.STRONG_FIT_THRESHOLD

# Statuses that mean an application actually went out. "saved" and "dismissed" are not
# applications, and counting them would inflate the one number a student uses to judge their
# own progress.
APPLIED_OR_BEYOND = ("applied", "assessment", "interview", "offer", "rejected", "closed")

# How search results may be ordered. "best_match" is the ranked order the engine produces;
# "newest" and "salary" re-order the same result set without re-scoring it.
SORT_OPTIONS = ("best_match", "newest", "salary")

# The most tracked applications one student's tracker will return in a single read. High
# enough that no real student hits it, low enough that a runaway or scripted account cannot
# make the endpoint read an unbounded number of rows.
MAX_TRACKER_ROWS = 500

# The same, for saved searches. Generous rather than tight for the same reason: there is no
# pagination on that screen, so anything this cap hides is hidden silently.
MAX_SAVED_SEARCHES = 200

# Stored column widths for the two listing fields that cannot survive truncation. Kept beside
# the model rather than read from it so a widening of the column is a deliberate edit here too.
_MAX_URL_LENGTH = 1000
_MAX_PROVIDER_ID_LENGTH = 255

# The `provider` value on a listing a student typed in themselves. Not a JobProvider: nothing
# in providers/factory.py answers to this name, so it can never collide with a configured
# adapter, and `build_job_provider` would fall through to NullJobProvider if anyone tried.
#
# The same word SAVED_JOB_SOURCES already uses for the same fact, rather than a second
# vocabulary meaning the same thing one layer down.
MANUAL_PROVIDER = "manual"


class JobHuntError(Exception):
    """Base for errors this module raises that the API maps to 4xx."""


class SearchProfileIncompleteError(JobHuntError):
    """Neither a target role nor a resume is set, so there is nothing to search on. A real
    product state with its own UI (the setup screen), not an internal failure."""


class ListingNotFoundError(JobHuntError):
    pass


class SavedSearchNotFoundError(JobHuntError):
    pass


class SavedJobNotFoundError(JobHuntError):
    pass


class InvalidJobUrlError(JobHuntError):
    """A manually entered job carried a link this app will not store."""


class InvalidStatusError(JobHuntError):
    pass


# -- Search profile -------------------------------------------------------------------------


def get_or_create_search_profile(db: Session, profile_id: int) -> JobSearchProfile:
    """One row per profile, created empty on first read.

    Creating on read rather than at signup keeps the table free of rows for the many students
    who never open Job Hunt, while letting every downstream call assume a row exists.
    """
    existing = (
        db.query(JobSearchProfile).filter(JobSearchProfile.profile_id == profile_id).first()
    )
    if existing is not None:
        return existing

    search_profile = JobSearchProfile(
        profile_id=profile_id,
        normalized_skills=[],
        declared_skills=[],
        preferred_companies=[],
        signal_provenance={},
    )
    db.add(search_profile)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent first-opens of Job Hunt. The unique constraint is the authority; the
        # loser re-reads the winner's row rather than surfacing a 500. Same pattern as
        # readiness_snapshot's upsert race.
        db.rollback()
        existing = (
            db.query(JobSearchProfile).filter(JobSearchProfile.profile_id == profile_id).first()
        )
        if existing is None:
            raise
        return existing
    db.refresh(search_profile)
    return search_profile


def _resume_skills(db: Session, resume_id: int, profile_id: int) -> list[str]:
    """Extracted skills for one resume the caller owns. Ownership is re-checked here rather
    than trusted from the caller, so a resume_id belonging to another student can never seed
    this student's search profile."""
    resume = (
        db.query(Resume)
        .filter(Resume.id == resume_id, Resume.profile_id == profile_id)
        .first()
    )
    if resume is None:
        return []
    analysis = db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume.id).first()
    if analysis is None:
        return []
    return [s for s in analysis.extracted_skills if isinstance(s, str)]


def update_search_profile(
    db: Session,
    profile_id: int,
    *,
    resume_id: Optional[int] = None,
    target_role_id: Optional[int] = None,
    target_role_title: Optional[str] = None,
    declared_skills: Optional[list[str]] = None,
    preferred_location: Optional[str] = None,
    work_mode: Optional[str] = None,
    experience_level: Optional[str] = None,
    employment_type: Optional[str] = None,
    salary_min_expected: Optional[int] = None,
    salary_currency: Optional[str] = None,
    preferred_companies: Optional[list[str]] = None,
    open_to_relocation: Optional[bool] = None,
    clear_resume: bool = False,
    clear_role: bool = False,
) -> JobSearchProfile:
    """Apply a partial update, recording where each changed signal came from.

    Every parameter defaults to None meaning "leave alone", which is what makes this a genuine
    PATCH -- a student editing only their preferred location must not silently clear their
    resume link. Clearing is therefore explicit (`clear_resume`/`clear_role`) rather than
    expressed as None, since None already means "unchanged".
    """
    search_profile = get_or_create_search_profile(db, profile_id)
    provenance = dict(search_profile.signal_provenance or {})

    if clear_resume:
        search_profile.resume_id = None
        provenance.pop("resume_skills", None)
    elif resume_id is not None:
        search_profile.resume_id = resume_id
        extracted = _resume_skills(db, resume_id, profile_id)
        if extracted:
            # Resume-derived skills are merged with, never allowed to overwrite, what the
            # student typed themselves: explicit input outranks extraction (spec section 3).
            merged = list(search_profile.declared_skills or []) + extracted
            search_profile.normalized_skills = normalization.normalize_skills(merged)
            provenance["resume_skills"] = "resume"

    if clear_role:
        search_profile.target_role_id = None
        search_profile.target_role_title = None
        provenance.pop("target_role_title", None)
    else:
        if target_role_id is not None:
            search_profile.target_role_id = target_role_id
            role = db.get(Role, target_role_id)
            if role is not None and not target_role_title:
                search_profile.target_role_title = role.name
                provenance["target_role_title"] = "explicit"
        if target_role_title is not None:
            search_profile.target_role_title = target_role_title
            provenance["target_role_title"] = "explicit"

    if declared_skills is not None:
        search_profile.declared_skills = declared_skills
        combined = list(declared_skills)
        if search_profile.resume_id is not None:
            combined += _resume_skills(db, search_profile.resume_id, profile_id)
        search_profile.normalized_skills = normalization.normalize_skills(combined)
        provenance["declared_skills"] = "explicit"

    for field, value, key in (
        ("preferred_location", preferred_location, "preferred_location"),
        ("work_mode", work_mode, "work_mode"),
        ("experience_level", experience_level, "experience_level"),
        ("employment_type", employment_type, "employment_type"),
        ("salary_min_expected", salary_min_expected, "salary_min_expected"),
        ("salary_currency", salary_currency, "salary_currency"),
        ("open_to_relocation", open_to_relocation, "open_to_relocation"),
    ):
        if value is not None:
            setattr(search_profile, field, value)
            provenance[key] = "explicit"

    if preferred_companies is not None:
        search_profile.preferred_companies = preferred_companies
        provenance["preferred_companies"] = "explicit"

    search_profile.signal_provenance = provenance
    search_profile.updated_at = _utcnow()
    db.commit()
    db.refresh(search_profile)
    return search_profile


def suggest_roles_from_resume(db: Session, profile_id: int, resume_id: int) -> list[str]:
    """Conservative role suggestions from a resume, for the student to confirm.

    Spec section 3: "If only a resume exists, infer possible roles conservatively and let the
    student confirm them." Two rules keep this honest. Suggestions are drawn only from
    experience titles the resume actually contains -- never guessed from skills, which would
    tell a student who lists Python that they are a Data Scientist. And they are returned as
    *suggestions* for an explicit confirmation step; nothing here writes target_role_title.
    """
    resume = (
        db.query(Resume).filter(Resume.id == resume_id, Resume.profile_id == profile_id).first()
    )
    if resume is None:
        return []
    analysis = db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume.id).first()
    if analysis is None:
        return []

    suggestions: dict[str, None] = {}
    for entry in analysis.extracted_experience or []:
        title = entry.get("title") if isinstance(entry, dict) else entry
        if not isinstance(title, str):
            continue
        family = normalization.role_family(title)
        if family is not None:
            suggestions.setdefault(family, None)
    return list(suggestions)


# -- Search ---------------------------------------------------------------------------------


class SearchOutcome:
    """What one search produced. A plain object rather than a tuple so the API layer reads
    clearly and new fields do not silently reorder anything."""

    def __init__(
        self,
        listings: list[JobListing],
        matches: dict[int, JobListingMatch],
        total_available: Optional[int],
        provider_name: str,
        attribution_text: str,
        attribution_url: Optional[str],
        fetched_at: datetime.datetime,
        duplicates_removed: int,
        evidence_states: Optional[dict[str, str]] = None,
    ) -> None:
        self.listings = listings
        self.matches = matches
        self.total_available = total_available
        self.provider_name = provider_name
        self.attribution_text = attribution_text
        self.attribution_url = attribution_url
        self.fetched_at = fetched_at
        self.duplicates_removed = duplicates_removed
        # Skill -> evidence_engine state, computed once for the whole page (see
        # evidence_states_for_skills). Carried on the outcome so the API layer can bucket every
        # result's skills without re-querying the student's evidence sources per listing.
        self.evidence_states = evidence_states or {}


def search(
    db: Session,
    profile: StudentProfile,
    provider: JobProvider,
    *,
    keywords: Optional[str] = None,
    location: Optional[str] = None,
    work_mode: Optional[str] = None,
    employment_type: Optional[str] = None,
    experience_level: Optional[str] = None,
    salary_min: Optional[int] = None,
    max_days_old: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
    sort: str = "best_match",
    end_user_ip: Optional[str] = None,
    end_user_agent: Optional[str] = None,
) -> SearchOutcome:
    """Run a live provider search, persist the listings, and score them for this student.

    Request filters take precedence over stored preferences for this one search, without
    overwriting the saved profile -- a student trying "remote" once should not have their
    profile silently rewritten.
    """
    search_profile = get_or_create_search_profile(db, profile.id)

    effective_keywords = keywords or search_profile.target_role_title
    if not effective_keywords and search_profile.resume_id is None:
        raise SearchProfileIncompleteError(
            "Add a target role or a resume before searching for jobs"
        )

    query = ProviderSearchQuery(
        keywords=effective_keywords,
        location=location or search_profile.preferred_location,
        remote_only=(work_mode or search_profile.work_mode) == "remote",
        employment_type=employment_type or search_profile.employment_type,
        experience_level=experience_level or search_profile.experience_level,
        salary_min=salary_min if salary_min is not None else search_profile.salary_min_expected,
        max_days_old=max_days_old,
        page=min(max(page, 1), MAX_PAGE),
        page_size=min(max(page_size, 1), MAX_PAGE_SIZE),
        end_user_ip=end_user_ip,
        end_user_agent=end_user_agent,
    )

    # The deployment's own budget for this provider's key, checked before the call rather
    # than discovered from a 429 afterwards. It is a separate limit from the student's
    # entitlement and answers a different question -- see provider_budget's module docstring --
    # and it is why a premium subscription can be genuinely unlimited without implying the
    # provider's key is. Providers that publish no per-day ceiling are not metered at all.
    #
    # getattr rather than a direct attribute read: the provider arrives through a FastAPI
    # dependency and may legitimately be a duck-typed object that predates this attribute.
    # Absent means the same thing the base class's default means -- no documented per-day
    # ceiling, so nothing to meter -- rather than an AttributeError mid-search.
    provider_budget.reserve_call(
        db, provider.name, getattr(provider, "daily_call_budget", None)
    )

    # Deliberately not wrapped: every JobProviderError subclass propagates to the API layer,
    # which maps each to its own status and message. Catching here would collapse "quota
    # exhausted" and "credentials revoked" into one indistinguishable outcome.
    result = provider.search(query)

    unique_jobs = dedupe.deduplicate(result.jobs)
    duplicates_removed = len(result.jobs) - len(unique_jobs)

    fetched_at = _utcnow()
    listings: list[JobListing] = []
    for job in unique_jobs:
        listing = _upsert_listing(db, job, provider, fetched_at)
        if listing is not None:
            listings.append(listing)
    db.commit()

    matches = {
        listing.id: _upsert_match(db, search_profile, listing) for listing in listings
    }
    db.commit()

    # Evidence strength as a ranking signal (spec phase 6). Fetched once for the union of
    # every skill across the page -- see evidence_states_for_skills for why per-listing calls
    # are not acceptable here.
    all_match_skills = [
        skill
        for match in matches.values()
        for skill in list(match.matched_skills or []) + list(match.missing_skills or [])
    ]
    evidence_states = evidence_states_for_skills(db, search_profile.profile_id, all_match_skills)
    verified_counts = {
        listing_id: provenance.verified_count(
            build_skill_provenance(search_profile, match, evidence_states)
        )
        for listing_id, match in matches.items()
    }
    # A job at a company the student named outranks an equally-scored one elsewhere. This is
    # ordering, not scoring: the number stays a statement about fit, and a preferred employer
    # can never make a poor match look like a good one.
    wanted_companies = tuple(search_profile.preferred_companies or [])
    preferred_company = {
        listing.id: matching.matches_preferred_company(listing.company, wanted_companies)
        for listing in listings
    }

    # Ordering. "best_match" is the engine's own ranking: highest score first, then a company
    # the student named, then the job whose required skills they can actually prove, then the
    # job we could evaluate on the most evidence, then id.
    #
    # The alternatives re-order this same scored set rather than re-scoring it, so switching
    # sort never changes a single match percentage -- the student sees the same jobs with the
    # same numbers in a different order, which is the only behaviour that keeps a sort control
    # trustworthy.
    #
    # That middle term is not cosmetic; it fixes a real mis-ranking. Because an unknown factor
    # is excluded from the weighted mean rather than scored as zero (see matching.py), a job
    # whose title we do not recognise has no role_alignment component at all -- so an advert
    # for "Chief Financial Officer" could tie on 1.0 with a confirmed "Software Engineer"
    # match for a student targeting software engineering, purely because *fewer* things about
    # it were knowable. Found by a ranking test, not by inspection.
    #
    # Preferring the better-evidenced job breaks that tie without inventing a score for the
    # unknown factor, which is what the honest alternative would have required. listing.id
    # remains the final tie-break so identical requests keep a stable order instead of
    # reshuffling on every refresh.
    if sort == "newest":
        # posted_at first, falling back to fetched_at for the adverts that carry no posted
        # date -- ordering those last rather than treating a missing date as very old, which
        # would bury every listing from a provider that omits the field.
        listings.sort(
            key=lambda item: (
                item.posted_at is None,
                -(item.posted_at or item.fetched_at).timestamp(),
                item.id,
            )
        )
    elif sort == "salary":
        # Highest advertised floor first. A listing with no salary is not "zero salary" -- the
        # employer simply did not say -- so those sort after every priced one rather than
        # bottoming the list as if they paid nothing.
        listings.sort(
            key=lambda item: (
                item.salary_min is None and item.salary_max is None,
                -(item.salary_min or item.salary_max or 0),
                item.id,
            )
        )
    else:
        listings.sort(
            key=lambda item: (
                -matches[item.id].total_score,
                -int(preferred_company.get(item.id, False)),
                -verified_counts[item.id],
                -len(matches[item.id].component_scores or {}),
                item.id,
            )
        )

    return SearchOutcome(
        listings=listings,
        matches=matches,
        total_available=result.total_available,
        provider_name=provider.name,
        attribution_text=provider.attribution_text,
        attribution_url=provider.attribution_url,
        fetched_at=fetched_at,
        duplicates_removed=duplicates_removed,
        evidence_states=evidence_states,
    )


def _upsert_listing(
    db: Session, job: ProviderJob, provider: JobProvider, fetched_at: datetime.datetime
) -> Optional[JobListing]:
    """Insert or refresh one listing. Returns None for a listing we must not show.

    The URL is validated here rather than at render time because this is the single point
    every listing passes through -- a listing whose URL is not a plain public http(s) link is
    dropped entirely instead of being stored and filtered later by every reader.
    """
    safe_url = normalization.safe_external_url(job.url)
    if safe_url is None:
        logger.warning(
            "Job Hunt: dropped a %s listing whose URL failed validation (id=%s)",
            job.provider,
            job.provider_job_id,
        )
        return None

    # Column widths are enforced here for the same reason the URL is: this is the one point
    # every listing passes through.
    #
    # SQLite ignores String(n) entirely; PostgreSQL raises StringDataRightTruncation. So a
    # provider returning one over-long field would pass every test in this repository and then
    # 500 the whole search page in production -- and the values are provider-controlled, not
    # ours. A malformed Careerjet response carrying salary_currency_code="RUPEES-INDIAN" (13
    # characters into a String(3) column) is enough.
    if len(safe_url) > _MAX_URL_LENGTH or len(job.provider_job_id or "") > _MAX_PROVIDER_ID_LENGTH:
        # Identity and link, both unusable if truncated: a clipped URL points somewhere else,
        # and a clipped id would collide with a different advert under the uniqueness
        # constraint. Dropped whole, like a listing with an invalid URL.
        logger.warning(
            "Job Hunt: dropped a %s listing whose url/id exceeds the stored column width (id=%s)",
            job.provider,
            (job.provider_job_id or "")[:60],
        )
        return None

    existing = _find_listing(db, job.provider, job.provider_job_id)
    # Work mode is the one attribute neither provider exposes as a field, so it is derived from
    # the advert's own title and description -- provider-supplied text, not an invention -- and
    # stays None when the text does not say.
    work_mode = _fit(job.work_mode or normalization.work_mode_from_text(job.title, job.description), 20)
    seniority = _fit(job.seniority or normalization.seniority_from_title(job.title), 30)
    expires_at = fetched_at + provider.cache_ttl

    # Display text is truncated rather than dropped -- no real advert comes near these limits,
    # and losing a job over a long title would be the worse trade. A currency code that does
    # not fit becomes None, because a clipped code would be a *different* currency and this
    # module never guesses at a value it does not have.
    title = _fit(job.title, 500)
    company = _fit(job.company, 300)
    location = _fit(job.location, 300)
    employment_type = _fit(job.employment_type, 30)
    salary_currency = _exact_or_none(job.salary_currency, 3)
    salary_period = _exact_or_none(job.salary_period, 10)

    if existing is not None:
        existing.url = safe_url
        existing.title = title
        existing.company = company
        existing.location = location
        existing.work_mode = work_mode
        existing.employment_type = employment_type
        existing.seniority = seniority
        existing.salary_min = job.salary_min
        existing.salary_max = job.salary_max
        existing.salary_currency = salary_currency
        existing.salary_period = salary_period
        existing.salary_is_predicted = job.salary_is_predicted
        existing.description = job.description
        existing.posted_at = job.posted_at
        existing.fetched_at = fetched_at
        existing.expires_at = expires_at
        return existing

    listing = JobListing(
        provider=job.provider,
        provider_job_id=job.provider_job_id,
        url=safe_url,
        title=title,
        company=company,
        location=location,
        work_mode=work_mode,
        employment_type=employment_type,
        seniority=seniority,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_currency=salary_currency,
        salary_period=salary_period,
        salary_is_predicted=job.salary_is_predicted,
        description=job.description,
        posted_at=job.posted_at,
        fetched_at=fetched_at,
        expires_at=expires_at,
    )
    # SAVEPOINT, not a plain flush-and-rollback.
    #
    # This runs in a LOOP over every advert in one search, and the loop commits once at the
    # end. A bare `db.rollback()` in the except branch would therefore not undo just the
    # duplicate -- it would undo the whole transaction, discarding every listing already
    # flushed earlier in the same loop. Two students searching the same keywords at the same
    # moment is enough to trigger it, and the result was a page that silently lost most of its
    # jobs. `begin_nested` scopes the undo to this one INSERT.
    #
    # `db.add` goes inside the block on purpose: an object added outside it survives the
    # savepoint rollback as a pending insert and would be retried -- and fail again -- at the
    # next commit.
    try:
        with db.begin_nested():
            db.add(listing)
            db.flush()
    except IntegrityError:
        # Another request inserted the same advert between the SELECT and the INSERT. The
        # unique constraint is the authority; adopt the row that won.
        return _find_listing(db, job.provider, job.provider_job_id)
    return listing


def _fit(value: Optional[str], limit: int) -> Optional[str]:
    """Display text clipped to what the column holds. None stays None."""
    if value is None:
        return None
    return value[:limit]


def _exact_or_none(value: Optional[str], limit: int) -> Optional[str]:
    """A coded value, or nothing. Unlike _fit this never clips: half of "RUPEES-INDIAN" is
    "RUP", which is not a shorter way of saying the same thing -- it is a different claim."""
    if value is None or len(value) > limit:
        return None
    return value


def _find_listing(db: Session, provider_name: str, provider_job_id: str) -> Optional[JobListing]:
    """The stored copy of one advert, or None.

    Its own function because _upsert_listing asks the same question twice -- once before
    inserting and once after losing the race to insert -- and the two must stay the same
    question.
    """
    return (
        db.query(JobListing)
        .filter(
            JobListing.provider == provider_name,
            JobListing.provider_job_id == provider_job_id,
        )
        .first()
    )


def build_match_input(
    search_profile: JobSearchProfile, listing: JobListing
) -> matching.MatchInput:
    """Assemble the engine's inputs from stored rows. Separate from scoring so the mapping is
    testable on its own and so the engine keeps no knowledge of the ORM."""
    job_skills = normalization.extract_job_skills(listing.title, listing.description)
    return matching.MatchInput(
        student_skills=list(search_profile.normalized_skills or []),
        student_role_family=normalization.role_family(search_profile.target_role_title),
        student_experience_level=normalization.normalize_experience_level(
            search_profile.experience_level
        ),
        preferred_location=search_profile.preferred_location,
        preferred_work_mode=search_profile.work_mode,
        preferred_employment_type=search_profile.employment_type,
        open_to_relocation=search_profile.open_to_relocation,
        preferred_companies=tuple(search_profile.preferred_companies or []),
        job_title=listing.title,
        job_company=listing.company,
        job_skills=job_skills,
        job_location=listing.location,
        job_work_mode=listing.work_mode,
        job_employment_type=listing.employment_type,
        job_seniority=listing.seniority,
        job_posted_at=listing.posted_at,
    )


def _upsert_match(
    db: Session, search_profile: JobSearchProfile, listing: JobListing
) -> JobListingMatch:
    result = matching.score_match(build_match_input(search_profile, listing))

    existing = _find_match(db, search_profile.profile_id, listing.id)
    if existing is not None:
        existing.total_score = result.total_score
        existing.score_version = result.score_version
        existing.component_scores = result.component_scores
        existing.matched_skills = result.matched_skills
        existing.missing_skills = result.missing_skills
        existing.unknown_factors = result.unknown_factors
        existing.rationale = result.rationale
        existing.calculated_at = _utcnow()
        return existing

    match = JobListingMatch(
        profile_id=search_profile.profile_id,
        listing_id=listing.id,
        total_score=result.total_score,
        score_version=result.score_version,
        component_scores=result.component_scores,
        matched_skills=result.matched_skills,
        missing_skills=result.missing_skills,
        unknown_factors=result.unknown_factors,
        rationale=result.rationale,
    )
    # Scoped to a savepoint for the same reason as _upsert_listing above: this is also called
    # in a loop that commits once at the end, so a plain rollback here would throw away every
    # match already computed for this page.
    try:
        with db.begin_nested():
            db.add(match)
            db.flush()
    except IntegrityError:
        existing = _find_match(db, search_profile.profile_id, listing.id)
        if existing is None:
            raise
        return existing
    return match


def _find_match(db: Session, profile_id: int, listing_id: int) -> Optional[JobListingMatch]:
    """This student's stored match for one listing, or None. Asked both before inserting and
    after losing the race to insert -- see _find_listing."""
    return (
        db.query(JobListingMatch)
        .filter(
            JobListingMatch.profile_id == profile_id,
            JobListingMatch.listing_id == listing_id,
        )
        .first()
    )


# -- Evidence provenance --------------------------------------------------------------------


def evidence_states_for_skills(db: Session, profile_id: int, skills: list[str]) -> dict[str, str]:
    """Map each skill to its evidence_engine state, in ONE pass over the student's sources.

    evidence_engine.get_all_skill_evidence fetches every assessment, project, interview and
    resume row for the profile once and matches the whole skill list against that in-memory
    snapshot. Calling the single-skill form per skill -- or this function per listing -- would
    turn a 20-result page into dozens of identical source queries, which is exactly the N+1
    the search path has to stay clear of. So callers collect the union of skills across every
    listing first and call this once.
    """
    unique = list(dict.fromkeys(skill for skill in skills if skill))
    if not unique:
        return {}
    return {
        view.skill: view.state
        for view in evidence_engine.get_all_skill_evidence(db, profile_id, unique)
    }


def build_skill_provenance(
    search_profile: JobSearchProfile,
    match: JobListingMatch,
    evidence_states: dict[str, str],
) -> list[provenance.SkillProvenance]:
    """Bucket one match's skills. Computed on read, never stored.

    Deliberately not a column on JobListingMatch: evidence changes underneath a stored match
    every time the student passes an assessment or finishes a project, so a persisted
    provenance would start telling the student their skill is unproven hours after they proved
    it. Every other evidence-derived view in this codebase (Career Twin, Skill Passport) is
    live-computed for the same reason.
    """
    return provenance.classify(
        list(match.matched_skills or []),
        list(match.missing_skills or []),
        evidence_states,
        student_has_skills=bool(search_profile.normalized_skills),
    )


def assert_listing_visible(listing: JobListing, profile_id: int) -> None:
    """Raise unless this student may read this listing.

    Two kinds of row live in job_listings and they have opposite privacy.

    A provider advert is public content: many students search the same keywords and match
    against the same job, and there is nothing in it that belongs to any one of them. Those
    rows stay readable by anyone, which is what makes a shared listing table correct in the
    first place.

    A manually entered listing is the student's own writing -- the company a senior referred
    them to, the description they pasted out of an email. `created_by_profile_id` is set on
    exactly those, and this is the check that keeps them private.

    ListingNotFoundError rather than a forbidden error, matching every other ownership check in
    this module: answering "that exists but is not yours" would confirm an id by probing.
    """
    owner = listing.created_by_profile_id
    if owner is not None and owner != profile_id:
        raise ListingNotFoundError(f"Job listing {listing.id} not found")


def get_listing_for_profile(
    db: Session, profile_id: int, listing_id: int
) -> tuple[JobListing, JobListingMatch]:
    """One listing plus this student's match for it.

    The match is recomputed when it is missing or was produced by an older engine version, so a
    student never sees a score today's weights would not produce. A provider listing is not
    profile-owned, so one this student has never searched is still readable -- there is nothing
    private in it -- but a manually entered listing belongs to whoever wrote it, and the match
    returned is always and only theirs either way.
    """
    listing = db.get(JobListing, listing_id)
    if listing is None:
        raise ListingNotFoundError(f"Job listing {listing_id} not found")
    assert_listing_visible(listing, profile_id)

    search_profile = get_or_create_search_profile(db, profile_id)
    match = (
        db.query(JobListingMatch)
        .filter(
            JobListingMatch.profile_id == profile_id,
            JobListingMatch.listing_id == listing_id,
        )
        .first()
    )
    if match is None or match.score_version != matching.SCORE_VERSION:
        match = _upsert_match(db, search_profile, listing)
        db.commit()
    return listing, match


def is_stale(listing: JobListing, now: Optional[datetime.datetime] = None) -> bool:
    """Whether the cached copy has outlived its provider's retention window. Surfaced to the
    student as a "last checked" caveat rather than hidden, per spec section 4."""
    if listing.expires_at is None:
        return False
    return (now or _utcnow()) > listing.expires_at


# -- Manually entered jobs ------------------------------------------------------------------


def _manual_listing_identity(profile_id: int, safe_url: Optional[str]) -> str:
    """The `provider_job_id` for one manually entered job.

    Two jobs are the same job when they are at the same link. That is the only identity a
    student's own typing gives us, and it is a real one: two different openings at one company
    have two different pages, so a URL match is an exact claim rather than a guess. Deriving
    the id from it means the UNIQUE (provider, provider_job_id) constraint that already
    deduplicates provider adverts deduplicates these too -- and arbitrates the race between two
    concurrent submissions -- with no second mechanism and no extra query.

    Scoped by profile so it cannot collide across students. Without that prefix, two students
    adding the same public job posting would land on one shared row, and the second would read
    the first student's typing.

    With no URL there is no key, so the id is opaque and unique and every submission creates
    its own row. That is deliberate rather than a gap: a student may genuinely be tracking two
    different campus roles at the same company with the same title, and nothing distinguishes
    them. Guessing that the second is a duplicate of the first would silently discard a real
    application. Fuzzy matching on title and company was considered and rejected for exactly
    that reason.
    """
    if safe_url is None:
        return f"{profile_id}:{uuid.uuid4().hex}"
    digest = hashlib.sha256(safe_url.encode("utf-8")).hexdigest()[:40]
    return f"{profile_id}:{digest}"


def add_manual_job(
    db: Session,
    profile_id: int,
    *,
    title: str,
    company: Optional[str] = None,
    location: Optional[str] = None,
    url: Optional[str] = None,
    description: Optional[str] = None,
    status: str = "saved",
) -> SavedJob:
    """Record a job the student found somewhere this app cannot search, and start tracking it.

    This is the one creation path for a JobListing that does not come from a provider, and it
    exists because the entire outcome loop -- listing, saved job, career goal, applied,
    interview, offer -- hangs off a listing row. Until this function, that row could only be
    produced by a paid search against a shared quota, so a student whose jobs came from campus
    placement or a referral could not use the tracker at all.

    Creating the listing and tracking it are one operation rather than two calls. An untracked
    manual listing would be unreachable by construction: no search returns it, and its owner
    would have no way back to it. It would also be a row of a student's own text with nothing
    pointing at it, which is the shape that survives cleanup.

    Saving is delegated to save_job rather than reimplemented, so provenance, the applied_at
    stamp, the per-profile unique constraint and the concurrent-save recovery are all the same
    code paths a provider-sourced save uses. `source` is fixed here rather than taken from the
    caller: where this job came from is a fact the server knows, not a claim the client makes.

    `status` is honoured on creation because a student who finds this product after already
    interviewing must be able to say so; forcing every manual job to start at "saved" would
    make them walk a real application backwards through a pipeline it has already passed.
    """
    if status not in SAVED_JOB_STATUSES:
        # Checked before anything is written. save_job would reject it too, but by then the
        # listing would already exist with nothing tracking it.
        raise InvalidStatusError(f"Unknown status {status!r}")

    safe_url: Optional[str] = None
    if url is not None and url.strip():
        safe_url = normalization.safe_external_url(url)
        if safe_url is None:
            # A provider listing with a bad URL is dropped silently, because one bad advert
            # must not fail a page of twenty. Here the student typed it, so silence would look
            # like the link was accepted. They are told instead.
            raise InvalidJobUrlError(
                "That link could not be saved. Enter a full http:// or https:// web address, "
                "or leave it empty."
            )
        if len(safe_url) > _MAX_URL_LENGTH:
            raise InvalidJobUrlError("That link is too long to save.")

    provider_job_id = _manual_listing_identity(profile_id, safe_url)
    listing = _find_listing(db, MANUAL_PROVIDER, provider_job_id)
    if listing is None:
        listing = JobListing(
            provider=MANUAL_PROVIDER,
            provider_job_id=provider_job_id,
            created_by_profile_id=profile_id,
            url=safe_url,
            title=_fit(title, 500),
            company=_fit(company, 300),
            location=_fit(location, 300),
            description=description,
            # Nothing is derived from the text the student typed. work_mode and seniority are
            # inferred for provider adverts because a provider writes standardised copy at
            # scale; inferring "remote" or "senior" from one student's shorthand would put a
            # fact in their tracker that they never stated.
            fetched_at=_utcnow(),
            # No expiry. expires_at exists to stop a cached provider copy being presented as
            # current after that provider's retention window; nothing here was cached from
            # anyone, so this listing is never stale and is_stale correctly returns False.
            expires_at=None,
        )
        try:
            with db.begin_nested():
                db.add(listing)
                db.flush()
        except IntegrityError:
            # Two submissions of the same link at once. The unique constraint is the
            # authority, exactly as it is on the provider path; adopt the row that won.
            listing = _find_listing(db, MANUAL_PROVIDER, provider_job_id)
            if listing is None:
                raise

    already_tracked = (
        db.query(SavedJob)
        .filter(SavedJob.profile_id == profile_id, SavedJob.listing_id == listing.id)
        .first()
    )
    if already_tracked is not None:
        # Re-adding a job already in the tracker returns what is there and changes nothing.
        # Passing `status` through to save_job here would be destructive: a student re-adding
        # a link they had already moved to "interview" would have it reset to whatever the
        # form defaulted to, silently rewinding a real application.
        db.commit()
        return already_tracked

    return save_job(db, profile_id, listing.id, status, source=MANUAL_PROVIDER)


# -- Saved jobs and application tracking ----------------------------------------------------


def save_job(
    db: Session,
    profile_id: int,
    listing_id: int,
    status: str = "saved",
    source: str = "manual",
    source_saved_search_id: Optional[int] = None,
) -> SavedJob:
    """Save, or move an already-saved job to a new status.

    `source` is recorded once, on the row that did not exist before, and never rewritten. A
    student who found a job through a saved search and later changes its status to "interview"
    still found it through that saved search; overwriting the provenance on every update would
    make the tracker rewrite its own history to say whatever happened most recently.
    """
    if status not in SAVED_JOB_STATUSES:
        raise InvalidStatusError(f"Unknown status {status!r}")
    if source not in SAVED_JOB_SOURCES:
        raise InvalidStatusError(f"Unknown source {source!r}")
    listing = db.get(JobListing, listing_id)
    if listing is None:
        raise ListingNotFoundError(f"Job listing {listing_id} not found")
    # Without this, the id in this request body is an IDOR on every manually entered job: a
    # student could save another student's typed-in listing and then read its title, company
    # and pasted description straight back out of their own tracker. Every other Job Hunt id
    # arrives in a path and is scoped by its handler; this one does not.
    assert_listing_visible(listing, profile_id)

    # `source_saved_search_id` arrives in a request body, unlike every other Job Hunt id,
    # which arrives in a path and is scoped by the handler. Stored unchecked it was a
    # cross-user reference: a student could tag their own saved job with someone else's
    # saved-search id and read that student's saved-search *name* back out of their own
    # tracker. Names are user-authored text, so that is a real disclosure, not a bare id.
    #
    # 404 rather than 403, the same choice create_alert makes for the same field: confirming
    # that another student's saved search exists is itself the leak.
    if source == "saved_search" and source_saved_search_id is not None:
        owned = (
            db.query(JobSavedSearch.id)
            .filter(
                JobSavedSearch.id == source_saved_search_id,
                JobSavedSearch.profile_id == profile_id,
            )
            .first()
        )
        if owned is None:
            raise SavedSearchNotFoundError(
                f"Saved search {source_saved_search_id} not found"
            )

    existing = (
        db.query(SavedJob)
        .filter(SavedJob.profile_id == profile_id, SavedJob.listing_id == listing_id)
        .first()
    )
    if existing is not None:
        # Saving an already-saved job is idempotent rather than an error: the student tapped
        # save twice, which is not a conflict worth showing them.
        existing.status = status
        existing.updated_at = _utcnow()
        if status == "applied" and existing.applied_at is None:
            existing.applied_at = _utcnow()
        db.commit()
        db.refresh(existing)
        return existing

    saved = SavedJob(
        profile_id=profile_id,
        listing_id=listing_id,
        status=status,
        source=source,
        # Only meaningful for source == "saved_search"; stored as NULL otherwise so a stale id
        # cannot make an unrelated row claim a search it never came from.
        source_saved_search_id=source_saved_search_id if source == "saved_search" else None,
        applied_at=_utcnow() if status == "applied" else None,
    )
    db.add(saved)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(SavedJob)
            .filter(SavedJob.profile_id == profile_id, SavedJob.listing_id == listing_id)
            .first()
        )
        if existing is None:
            raise
        return existing
    db.refresh(saved)
    return saved


def update_saved_job(
    db: Session,
    profile_id: int,
    saved_job_id: int,
    *,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    follow_up_date: Optional[datetime.date] = None,
) -> SavedJob:
    saved = (
        db.query(SavedJob)
        .filter(SavedJob.id == saved_job_id, SavedJob.profile_id == profile_id)
        .first()
    )
    if saved is None:
        # Deliberately indistinguishable from "does not exist" -- a row owned by someone else
        # must not be confirmable by its id (see tests/api/test_job_hunt_isolation.py).
        raise SavedJobNotFoundError(f"Saved job {saved_job_id} not found")

    if status is not None:
        if status not in SAVED_JOB_STATUSES:
            raise InvalidStatusError(f"Unknown status {status!r}")
        saved.status = status
        if status == "applied" and saved.applied_at is None:
            saved.applied_at = _utcnow()
    if notes is not None:
        saved.notes = notes
    if follow_up_date is not None:
        saved.follow_up_date = follow_up_date

    saved.updated_at = _utcnow()
    db.commit()
    db.refresh(saved)
    return saved


def delete_saved_job(db: Session, profile_id: int, saved_job_id: int) -> None:
    saved = (
        db.query(SavedJob)
        .filter(SavedJob.id == saved_job_id, SavedJob.profile_id == profile_id)
        .first()
    )
    if saved is None:
        raise SavedJobNotFoundError(f"Saved job {saved_job_id} not found")
    db.delete(saved)
    db.commit()


def list_saved_jobs(
    db: Session,
    profile_id: int,
    statuses: Optional[list[str]] = None,
    limit: int = MAX_TRACKER_ROWS,
) -> list[tuple[SavedJob, JobListing]]:
    """Saved rows joined to their listings, newest first.

    An explicit join rather than a lazy relationship walk: the tracker screen renders every
    row's title and company, so a per-row lazy load would be a guaranteed N+1 (spec section 15
    calls out exactly this).

    The default used to be 100, which is what the tracker endpoint got by not passing one. A
    student with more than a hundred tracked applications simply stopped seeing the oldest of
    them, with nothing on screen to say so -- and the tracker is the one place in the product
    where a missing row means a forgotten application. The default is now the documented
    ceiling, and the ceiling is clamped here rather than trusted from the caller.
    """
    query = (
        db.query(SavedJob, JobListing)
        .join(JobListing, JobListing.id == SavedJob.listing_id)
        .filter(SavedJob.profile_id == profile_id)
    )
    if statuses:
        query = query.filter(SavedJob.status.in_(statuses))
    return (
        query.order_by(SavedJob.updated_at.desc())
        .limit(max(1, min(limit, MAX_TRACKER_ROWS)))
        .all()
    )


def saved_jobs_for_listings(
    db: Session, profile_id: int, listing_ids: list[int]
) -> dict[int, SavedJob]:
    """This student's saved rows for exactly these listings, keyed by listing id.

    Search, home and saved-search-run all need to know which of the ~20 jobs on the page the
    student has already saved. Each of them previously answered that by loading the student's
    most recent 500 saved rows joined to their listings and building a dict from it -- three or
    four hundred rows of joined data read to annotate twenty, and wrong past the cap: a student
    with more than 500 saved jobs would see an older save reported as unsaved, and saving it
    again would silently reopen a row that was already in their tracker at a different status.

    Asking for the listings actually on the page is both smaller and exact at any tracker size.
    """
    if not listing_ids:
        return {}
    rows = (
        db.query(SavedJob)
        .filter(
            SavedJob.profile_id == profile_id,
            SavedJob.listing_id.in_(listing_ids),
        )
        .all()
    )
    return {row.listing_id: row for row in rows}


def get_saved_job_matches(
    db: Session, profile_id: int, listing_ids: list[int]
) -> dict[int, JobListingMatch]:
    """Matches for a set of listings in one query -- the companion to list_saved_jobs that
    keeps the tracker screen at two queries total rather than one per row.

    Rows produced by an older engine version are recomputed first, for the same reason
    get_listing_for_profile does it: a saved job the student has not reopened since a scoring
    change would otherwise sit in their tracker showing a number today's engine would not
    produce, next to jobs scored by the current one. Only stale rows are touched, and only
    once each -- the common case is that nothing is stale and this costs one extra comparison
    per row.
    """
    if not listing_ids:
        return {}
    rows = (
        db.query(JobListingMatch)
        .filter(
            JobListingMatch.profile_id == profile_id,
            JobListingMatch.listing_id.in_(listing_ids),
        )
        .all()
    )
    stale = [row for row in rows if row.score_version != matching.SCORE_VERSION]
    if stale:
        search_profile = get_or_create_search_profile(db, profile_id)
        # One query for every stale row's listing rather than one per row. Normally there are
        # no stale rows at all, but the run right after a SCORE_VERSION bump has every tracked
        # row stale at once -- up to MAX_TRACKER_ROWS single-row reads on one tracker open.
        listings = {
            listing.id: listing
            for listing in db.query(JobListing)
            .filter(JobListing.id.in_({row.listing_id for row in stale}))
            .all()
        }
        refreshed = []
        for row in stale:
            listing = listings.get(row.listing_id)
            if listing is None:
                # The shared listing was pruned out from under a saved row. Nothing to
                # rescore against, so the old row is left as-is rather than guessed at.
                continue
            refreshed.append(_upsert_match(db, search_profile, listing))
        if refreshed:
            db.commit()
        rows = [row for row in rows if row.score_version == matching.SCORE_VERSION] + refreshed
    return {row.listing_id: row for row in rows}


# -- Saved searches -------------------------------------------------------------------------


def create_saved_search(
    db: Session, profile_id: int, name: str, filters: dict, notify_enabled: bool = False
) -> JobSavedSearch:
    saved_search = JobSavedSearch(
        profile_id=profile_id,
        name=name,
        filters=filters,
        notify_enabled=notify_enabled,
    )
    db.add(saved_search)
    db.commit()
    db.refresh(saved_search)
    return saved_search


def list_saved_searches(db: Session, profile_id: int) -> list[JobSavedSearch]:
    """Every saved search this student has, newest first.

    The cap was 50, which is the same silent-truncation shape the tracker had: there is no
    "load more" on the saved-searches screen, so the 51st search would simply not be there and
    nothing would say so. Raised to the documented ceiling below, which no realistic student
    reaches -- saved searches are created one at a time, by hand.

    Note what this is *not*: a limit on how many a student may create. Nothing caps that today,
    and inventing a number would be setting a product policy rather than fixing a defect.
    """
    return (
        db.query(JobSavedSearch)
        .filter(JobSavedSearch.profile_id == profile_id)
        .order_by(JobSavedSearch.created_at.desc())
        .limit(MAX_SAVED_SEARCHES)
        .all()
    )


def delete_saved_search(db: Session, profile_id: int, saved_search_id: int) -> None:
    """Delete a saved search, and settle the two things that point at it first.

    Both `saved_jobs.source_saved_search_id` and `job_alerts.saved_search_id` are real foreign
    keys. Deleting the parent without clearing them raises an IntegrityError on PostgreSQL --
    a 500 on an ordinary "remove this search" tap. It did not show up in tests because SQLite
    does not enforce foreign keys unless `PRAGMA foreign_keys=ON` is set, which nothing here
    sets; tests/api/test_job_hunt_fk_integrity.py turns it on for exactly this reason.

    The two dependants are settled differently, because they mean different things:

    * A tracked job keeps its row and its `source="saved_search"`, losing only the id. The
      student really did find that job through a search, and rewriting the provenance to
      "manual" would make the tracker falsify its own history. The display already handles a
      missing name (see _to_saved_read), so this is the behaviour the tracker was written for.

    * An alert bound to the search is deleted with it. Its filters were read live from the
      search, so a surviving alert would have nothing to search on -- it would sit in the
      student's list reporting "not configured" forever, which is worse than it being gone.
      Deliveries go first, since they reference the alert.
    """
    saved_search = (
        db.query(JobSavedSearch)
        .filter(
            JobSavedSearch.id == saved_search_id, JobSavedSearch.profile_id == profile_id
        )
        .first()
    )
    if saved_search is None:
        raise SavedJobNotFoundError(f"Saved search {saved_search_id} not found")

    db.query(SavedJob).filter(
        SavedJob.profile_id == profile_id,
        SavedJob.source_saved_search_id == saved_search.id,
    ).update({SavedJob.source_saved_search_id: None}, synchronize_session=False)

    orphaned_alert_ids = [
        row.id
        for row in db.query(JobAlert.id)
        .filter(
            JobAlert.profile_id == profile_id,
            JobAlert.saved_search_id == saved_search.id,
        )
        .all()
    ]
    if orphaned_alert_ids:
        db.query(JobAlertDelivery).filter(
            JobAlertDelivery.alert_id.in_(orphaned_alert_ids)
        ).delete(synchronize_session=False)
        db.query(JobAlert).filter(JobAlert.id.in_(orphaned_alert_ids)).delete(
            synchronize_session=False
        )

    db.delete(saved_search)
    db.commit()


def _utcnow() -> datetime.datetime:
    """Naive UTC, matching every other DateTime column in this schema."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# -- Saved-search execution -----------------------------------------------------------------


def get_saved_search(db: Session, profile_id: int, saved_search_id: int) -> JobSavedSearch:
    search = (
        db.query(JobSavedSearch)
        .filter(
            JobSavedSearch.id == saved_search_id,
            JobSavedSearch.profile_id == profile_id,
        )
        .first()
    )
    # 404 rather than 403: telling someone that another student's saved search exists is
    # itself a leak, however small.
    if search is None:
        raise SavedSearchNotFoundError(f"Saved search {saved_search_id} not found")
    return search


def run_saved_search(
    db: Session,
    profile: StudentProfile,
    provider: JobProvider,
    saved_search_id: int,
    *,
    end_user_ip: Optional[str] = None,
    end_user_agent: Optional[str] = None,
) -> tuple[JobSavedSearch, SearchOutcome]:
    """Re-run a stored search and record what happened.

    Run metadata is written on both paths, and differently. A completed provider call sets
    last_run_at, last_success_at and last_result_count together. A failure sets last_run_at and
    last_error_at and leaves last_result_count untouched, because overwriting a real previous
    count with a zero on a failed run would tell the student their search stopped matching
    anything when what actually happened is that we could not ask.
    """
    search = get_saved_search(db, profile.id, saved_search_id)
    filters = dict(search.filters or {})
    now = _utcnow()

    try:
        outcome = search_jobs_with_filters(
            db,
            profile,
            provider,
            filters,
            end_user_ip=end_user_ip,
            end_user_agent=end_user_agent,
        )
    except Exception:
        search.last_run_at = now
        search.last_error_at = now
        db.commit()
        raise

    search.last_run_at = now
    search.last_success_at = now
    search.last_result_count = len(outcome.listings)
    db.commit()
    db.refresh(search)
    return search, outcome


def search_jobs_with_filters(
    db: Session,
    profile: StudentProfile,
    provider: JobProvider,
    filters: dict,
    *,
    end_user_ip: Optional[str] = None,
    end_user_agent: Optional[str] = None,
) -> SearchOutcome:
    """`search` called with a stored filter dict.

    Unknown keys are ignored rather than passed through: a saved search written by an older
    version of the app must not be able to reach a parameter that did not exist when it was
    saved, and a filter dict is stored data, which is to say untrusted input.
    """
    return search(
        db,
        profile,
        provider,
        keywords=filters.get("keywords"),
        location=filters.get("location"),
        work_mode=filters.get("work_mode"),
        employment_type=filters.get("employment_type"),
        experience_level=filters.get("experience_level"),
        salary_min=filters.get("salary_min"),
        max_days_old=filters.get("max_days_old"),
        page=filters.get("page") or 1,
        page_size=filters.get("page_size") or 20,
        sort=filters.get("sort") or "best_match",
        end_user_ip=end_user_ip,
        end_user_agent=end_user_agent,
    )


# -- Home summary ---------------------------------------------------------------------------


def home_summary(db: Session, profile_id: int, now: Optional[datetime.datetime] = None) -> dict:
    """The three counts on the Job Hunt home screen.

    Every one is a real count over rows this student owns, and every one is honest about what
    it counts:

      new_jobs      listings fetched for anyone in the last 7 days that this student has a
                    computed match for. Not "jobs in the market" -- we have no idea how many
                    of those there are, and a number implying we did would be fabricated.
      best_matches  matches at or above the strong-fit threshold the ranking labels already
                    use, so the tile and the card badges cannot disagree.
      applied       saved jobs that have actually reached the applied stage or beyond.

    Returns zeros only when the underlying counts genuinely are zero; a student who has never
    searched gets zeros and the UI is expected to show the empty state rather than the tiles.
    """
    now = now or _utcnow()
    fresh_cutoff = now - datetime.timedelta(days=NEW_JOB_WINDOW_DAYS)

    new_jobs = (
        db.query(JobListingMatch)
        .join(JobListing, JobListing.id == JobListingMatch.listing_id)
        .filter(JobListingMatch.profile_id == profile_id)
        .filter(JobListing.fetched_at >= fresh_cutoff)
        .count()
    )
    best_matches = (
        db.query(JobListingMatch)
        .filter(JobListingMatch.profile_id == profile_id)
        .filter(JobListingMatch.total_score >= STRONG_FIT_THRESHOLD)
        .count()
    )
    applied = (
        db.query(SavedJob)
        .filter(SavedJob.profile_id == profile_id)
        .filter(SavedJob.status.in_(APPLIED_OR_BEYOND))
        .count()
    )
    saved = (
        db.query(SavedJob)
        .filter(SavedJob.profile_id == profile_id)
        .filter(SavedJob.status == "saved")
        .count()
    )
    return {
        "new_jobs": new_jobs,
        "best_matches": best_matches,
        "applied": applied,
        "saved": saved,
    }


def top_matches(
    db: Session, profile_id: int, limit: int = 5
) -> tuple[list[JobListing], dict[int, JobListingMatch]]:
    """The student's best already-scored jobs, for the home screen.

    Reads stored matches rather than searching, so opening the app costs no provider quota.
    Dismissed jobs are excluded -- recommending back a job the student explicitly hid is the
    fastest way to make a recommendation feel like it is not listening.

    Stale-versioned matches are excluded rather than recomputed: this is a summary, and
    silently rescoring a page of jobs on every app open would turn a cheap read into the most
    expensive call in the product. They reappear the moment the student searches or opens one.
    """
    # Dismissed jobs are excluded in SQL rather than fetched and filtered out afterwards.
    # Over-fetching by the number of dismissals meant the home screen's query grew with every
    # job the student ever hid -- an account that had dismissed five hundred jobs read five
    # hundred and five rows to show five.
    dismissed = (
        db.query(SavedJob.listing_id)
        .filter(SavedJob.profile_id == profile_id, SavedJob.status == "dismissed")
        .scalar_subquery()
    )
    rows = (
        db.query(JobListingMatch, JobListing)
        .join(JobListing, JobListing.id == JobListingMatch.listing_id)
        .filter(JobListingMatch.profile_id == profile_id)
        .filter(JobListingMatch.score_version == matching.SCORE_VERSION)
        .filter(JobListingMatch.listing_id.notin_(dismissed))
        .order_by(JobListingMatch.total_score.desc(), JobListing.id)
        .limit(max(1, limit))
        .all()
    )
    listings = [listing for _match, listing in rows]
    matches = {listing.id: match for match, listing in rows}
    return listings, matches
