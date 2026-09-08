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
import logging
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
from app.services.job_hunt import dedupe, matching, normalization, provenance
from app.services.job_hunt.providers.base import (
    JobProvider,
    ProviderJob,
    ProviderSearchQuery,
)

logger = logging.getLogger(__name__)

# Applied to a single search request. Above this the provider quota (Adzuna's documented
# default is 25 hits/minute) is burned by one student.
MAX_PAGE_SIZE = 50

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
        page=max(page, 1),
        page_size=min(max(page_size, 1), MAX_PAGE_SIZE),
        end_user_ip=end_user_ip,
        end_user_agent=end_user_agent,
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

    existing = (
        db.query(JobListing)
        .filter(
            JobListing.provider == job.provider,
            JobListing.provider_job_id == job.provider_job_id,
        )
        .first()
    )
    # Work mode is the one attribute neither provider exposes as a field, so it is derived from
    # the advert's own title and description -- provider-supplied text, not an invention -- and
    # stays None when the text does not say.
    work_mode = job.work_mode or normalization.work_mode_from_text(job.title, job.description)
    seniority = job.seniority or normalization.seniority_from_title(job.title)
    expires_at = fetched_at + provider.cache_ttl

    if existing is not None:
        existing.url = safe_url
        existing.title = job.title
        existing.company = job.company
        existing.location = job.location
        existing.work_mode = work_mode
        existing.employment_type = job.employment_type
        existing.seniority = seniority
        existing.salary_min = job.salary_min
        existing.salary_max = job.salary_max
        existing.salary_currency = job.salary_currency
        existing.salary_period = job.salary_period
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
        title=job.title,
        company=job.company,
        location=job.location,
        work_mode=work_mode,
        employment_type=job.employment_type,
        seniority=seniority,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_currency=job.salary_currency,
        salary_period=job.salary_period,
        salary_is_predicted=job.salary_is_predicted,
        description=job.description,
        posted_at=job.posted_at,
        fetched_at=fetched_at,
        expires_at=expires_at,
    )
    db.add(listing)
    try:
        db.flush()
    except IntegrityError:
        # Another request inserted the same advert between the SELECT and the INSERT. The
        # unique constraint is the authority; adopt the row that won.
        db.rollback()
        existing = (
            db.query(JobListing)
            .filter(
                JobListing.provider == job.provider,
                JobListing.provider_job_id == job.provider_job_id,
            )
            .first()
        )
        return existing
    return listing


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

    existing = (
        db.query(JobListingMatch)
        .filter(
            JobListingMatch.profile_id == search_profile.profile_id,
            JobListingMatch.listing_id == listing.id,
        )
        .first()
    )
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
    db.add(match)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(JobListingMatch)
            .filter(
                JobListingMatch.profile_id == search_profile.profile_id,
                JobListingMatch.listing_id == listing.id,
            )
            .first()
        )
        if existing is None:
            raise
        return existing
    return match


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


def get_listing_for_profile(
    db: Session, profile_id: int, listing_id: int
) -> tuple[JobListing, JobListingMatch]:
    """One listing plus this student's match for it.

    The match is recomputed when it is missing or was produced by an older engine version, so a
    student never sees a score today's weights would not produce. Listings themselves are not
    profile-owned, so a listing this student has never searched is still readable -- there is
    nothing private in it -- but the match returned is always and only theirs.
    """
    listing = db.get(JobListing, listing_id)
    if listing is None:
        raise ListingNotFoundError(f"Job listing {listing_id} not found")

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
    if db.get(JobListing, listing_id) is None:
        raise ListingNotFoundError(f"Job listing {listing_id} not found")

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
    db: Session, profile_id: int, statuses: Optional[list[str]] = None, limit: int = 100
) -> list[tuple[SavedJob, JobListing]]:
    """Saved rows joined to their listings, newest first.

    An explicit join rather than a lazy relationship walk: the tracker screen renders every
    row's title and company, so a per-row lazy load would be a guaranteed N+1 (spec section 15
    calls out exactly this).
    """
    query = (
        db.query(SavedJob, JobListing)
        .join(JobListing, JobListing.id == SavedJob.listing_id)
        .filter(SavedJob.profile_id == profile_id)
    )
    if statuses:
        query = query.filter(SavedJob.status.in_(statuses))
    return query.order_by(SavedJob.updated_at.desc()).limit(limit).all()


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
        refreshed = []
        for row in stale:
            listing = db.get(JobListing, row.listing_id)
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
    return (
        db.query(JobSavedSearch)
        .filter(JobSavedSearch.profile_id == profile_id)
        .order_by(JobSavedSearch.created_at.desc())
        .limit(50)
        .all()
    )


def delete_saved_search(db: Session, profile_id: int, saved_search_id: int) -> None:
    saved_search = (
        db.query(JobSavedSearch)
        .filter(
            JobSavedSearch.id == saved_search_id, JobSavedSearch.profile_id == profile_id
        )
        .first()
    )
    if saved_search is None:
        raise SavedJobNotFoundError(f"Saved search {saved_search_id} not found")
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
    dismissed = {
        row.listing_id
        for row in db.query(SavedJob.listing_id)
        .filter(SavedJob.profile_id == profile_id, SavedJob.status == "dismissed")
        .all()
    }
    rows = (
        db.query(JobListingMatch, JobListing)
        .join(JobListing, JobListing.id == JobListingMatch.listing_id)
        .filter(JobListingMatch.profile_id == profile_id)
        .filter(JobListingMatch.score_version == matching.SCORE_VERSION)
        .order_by(JobListingMatch.total_score.desc(), JobListing.id)
        .limit(max(1, limit) + len(dismissed))
        .all()
    )
    listings: list[JobListing] = []
    matches: dict[int, JobListingMatch] = {}
    for match, listing in rows:
        if listing.id in dismissed:
            continue
        listings.append(listing)
        matches[listing.id] = match
        if len(listings) >= limit:
            break
    return listings, matches
