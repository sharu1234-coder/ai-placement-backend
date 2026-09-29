"""Request/response shapes for Job Hunt.

Literal types rather than bare `str` on every enumerated field: FastAPI then rejects an
unknown work mode or pipeline status with a 422 before any handler runs, which keeps the
service layer free of defensive re-validation and means an invalid status can never reach the
database.
"""

import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

WorkMode = Literal["remote", "hybrid", "onsite"]
EmploymentType = Literal["full_time", "part_time", "contract", "internship", "temporary"]
ExperienceLevel = Literal["fresher", "junior", "mid", "senior", "lead"]
SavedJobStatus = Literal[
    "saved", "applied", "assessment", "interview", "offer", "rejected", "closed", "dismissed"
]


# -- Job Hunt profile -----------------------------------------------------------------------


class JobSearchProfileRead(BaseModel):
    resume_id: Optional[int] = None
    target_role_id: Optional[int] = None
    target_role_title: Optional[str] = None
    declared_skills: List[str] = []
    normalized_skills: List[str] = []
    preferred_location: Optional[str] = None
    work_mode: Optional[WorkMode] = None
    experience_level: Optional[ExperienceLevel] = None
    employment_type: Optional[EmploymentType] = None
    salary_min_expected: Optional[int] = None
    salary_currency: Optional[str] = None
    preferred_companies: List[str] = []
    open_to_relocation: Optional[bool] = None
    # Per-field origin: explicit | resume | evidence | inferred. Shown in the UI beside each
    # value so a student can see what we assumed about them and correct it (spec section 3).
    signal_provenance: dict = {}
    # False when neither a role nor a resume is set -- the client renders the setup screen
    # rather than an empty results list, which would imply we searched and found nothing.
    is_ready_to_search: bool = False
    updated_at: Optional[datetime.datetime] = None

    model_config = {"from_attributes": True}


class JobSearchProfileUpdate(BaseModel):
    """Every field optional and defaulting to None, meaning "leave unchanged".

    Clearing is therefore explicit rather than expressed as null, since null already means
    "not supplied" -- without this, a client sending a partial update would silently unset
    everything it omitted.
    """

    resume_id: Optional[int] = None
    target_role_id: Optional[int] = None
    target_role_title: Optional[str] = Field(default=None, max_length=255)
    declared_skills: Optional[List[str]] = Field(default=None, max_length=100)
    preferred_location: Optional[str] = Field(default=None, max_length=255)
    work_mode: Optional[WorkMode] = None
    experience_level: Optional[ExperienceLevel] = None
    employment_type: Optional[EmploymentType] = None
    salary_min_expected: Optional[int] = Field(default=None, ge=0)
    salary_currency: Optional[str] = Field(default=None, max_length=3)
    preferred_companies: Optional[List[str]] = Field(default=None, max_length=50)
    open_to_relocation: Optional[bool] = None
    clear_resume: bool = False
    clear_role: bool = False


class RoleSuggestionsRead(BaseModel):
    """Conservative suggestions the student must confirm -- never auto-applied."""

    suggested_role_families: List[str] = []


# -- Listings and matches -------------------------------------------------------------------


class JobListingRead(BaseModel):
    id: int
    provider: str
    # None only on a manually entered job with no link -- a campus placement, a referral, a
    # role someone described over email. The client hides the apply button rather than opening
    # nothing; it must never substitute a search URL or the company name.
    url: Optional[str] = None
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[WorkMode] = None
    employment_type: Optional[str] = None
    seniority: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: Optional[str] = None
    salary_period: Optional[str] = None
    # True when the provider itself says the figure is modelled. The UI must label it as an
    # estimate -- presenting a predicted salary as advertised would be fabricated data.
    salary_is_predicted: bool = False
    description: Optional[str] = None
    # None means the provider did not supply one. The client omits freshness entirely rather
    # than showing a placeholder date (spec section 12).
    posted_at: Optional[datetime.datetime] = None
    fetched_at: datetime.datetime
    is_stale: bool = False

    model_config = {"from_attributes": True}


class SkillProvenanceRead(BaseModel):
    """One skill an advert asks for, and how well the student can actually back it.

    The four statuses are the product's whole answer to "is this a claim or a proof": they
    come from app/services/job_hunt/provenance.py, which delegates the verified/not-verified
    judgement to the same Evidence Engine that Career Twin and the Skill Passport use. A
    Literal rather than a str so a typo in a new status is a startup failure, not a chip the
    client silently fails to recognise.
    """

    skill: str
    status: Literal["verified_evidence", "on_resume", "missing", "unclear"]
    # The evidence_engine state this was derived from. Null for a skill the student does not
    # have -- there is no evidence to name.
    evidence_state: Optional[
        Literal["claimed", "supported", "strong", "verified", "stale"]
    ] = None
    detail: Optional[str] = None


class JobMatchRead(BaseModel):
    total_score: float
    score_version: str
    component_scores: dict = {}
    matched_skills: List[str] = []
    missing_skills: List[str] = []
    # Factors the provider gave us nothing to judge on. Present so a mid-range score reads as
    # "we could not tell" instead of "you scored badly".
    unknown_factors: List[str] = []
    rationale: str = ""
    reasons: List[str] = []
    # None when there is no real evidence to judge readiness on (spec section 5).
    apply_readiness: Optional[Literal["ready", "stretch", "gap"]] = None
    # Every skill this advert asks for, bucketed as verified evidence / on your resume /
    # missing / unclear. Computed on read, never stored -- evidence changes underneath a
    # stored match the moment the student passes an assessment.
    skill_provenance: List[SkillProvenanceRead] = []
    # How many of the advert's skills the student can actually prove. A ranking signal and a
    # headline number, kept separate from total_score so proof strength never inflates fit.
    verified_skill_count: int = 0
    # True when the employer is one the student named. Reported separately from the score,
    # which never absorbs it -- a preferred employer orders two equal matches, it does not
    # make a poor one look good.
    preferred_company_match: bool = False
    # Honest fit label (spec phase 6). Never an outcome claim: "Best Match" means top of this
    # result set, not most likely to hire you. Null when nothing about the job was evaluable.
    ranking_label: Optional[
        Literal["best_match", "strong_fit", "stretch_opportunity", "skill_build_opportunity"]
    ] = None

    model_config = {"from_attributes": True}


class JobResultRead(BaseModel):
    listing: JobListingRead
    match: JobMatchRead
    # Present only if the student has already saved or dismissed this job, so the card can
    # render the right control without a second round trip.
    saved_job_id: Optional[int] = None
    saved_status: Optional[SavedJobStatus] = None


class JobSearchRequest(BaseModel):
    keywords: Optional[str] = Field(default=None, max_length=200)
    location: Optional[str] = Field(default=None, max_length=200)
    work_mode: Optional[WorkMode] = None
    employment_type: Optional[EmploymentType] = None
    experience_level: Optional[ExperienceLevel] = None
    salary_min: Optional[int] = Field(default=None, ge=0)
    max_days_old: Optional[int] = Field(default=None, ge=1, le=365)
    page: int = Field(default=1, ge=1, le=10)
    # Capped server-side too (job_hunt/service.MAX_PAGE_SIZE); the bound here gives the client
    # a clear 422 instead of a silently truncated page.
    page_size: int = Field(default=20, ge=1, le=50)
    # Re-orders the same scored result set; never re-scores it, so switching sort cannot change
    # a single match percentage.
    sort: Literal["best_match", "newest", "salary"] = "best_match"


class JobSearchResponse(BaseModel):
    results: List[JobResultRead] = []
    # None when the provider does not report a total. Never defaulted to len(results), which
    # would make page 1 of many look like the whole result set.
    total_available: Optional[int] = None
    page: int
    page_size: int
    provider: str
    # Contractually required by several providers, so it travels with the results rather than
    # being hardcoded in the client where it could drift from the provider actually in use.
    attribution_text: str = ""
    attribution_url: Optional[str] = None
    fetched_at: datetime.datetime
    duplicates_removed: int = 0
    score_version: str


# -- Saved jobs / application tracker -------------------------------------------------------


class ManualJobCreate(BaseModel):
    """A job the student found somewhere this app cannot search.

    Every field here already exists on JobListing. Nothing provider-internal is accepted --
    not `provider`, not `provider_job_id`, not ranking or search metadata -- because those
    describe how a listing was ingested, and this one was typed. `source` is absent for the
    same reason ownership is: the server knows this job was entered by hand, so letting the
    client assert it would be a claim, not a fact.

    Lengths match the stored columns exactly (title 500, company and location 300) rather than
    the Career Goal form's 200, because this writes to job_listings and a value that fits the
    schema but not the column is a 500 on PostgreSQL and a silent truncation on SQLite.
    """

    title: str = Field(min_length=1, max_length=500)
    company: Optional[str] = Field(default=None, max_length=300)
    location: Optional[str] = Field(default=None, max_length=300)
    # Stored as given if it is a plain http(s) link to a public host, and refused with a
    # message otherwise. Never fetched: a URL here is data, not an instruction to go and read
    # something (see job_hunt/normalization.py::safe_external_url for why that boundary is
    # drawn at storage time).
    url: Optional[str] = Field(default=None, max_length=1000)
    # The description column is TEXT and has no stored ceiling, so this bound is the only one.
    # It matches MAX_JOB_DESCRIPTION_LENGTH so a job description is refused at the same size
    # whichever door it comes through -- but for a different reason than that constant's:
    # nothing here is ever sent to an AI provider, so the limit is about unbounded storage of
    # untrusted input rather than about prompt size.
    description: Optional[str] = Field(default=None, max_length=20_000)
    # A student may find this product after already applying, or already interviewing. The
    # tracker supports every stage, so entry does too -- forcing "saved" first would make them
    # record something that is not true and then correct it.
    status: SavedJobStatus = "saved"

    @field_validator("title", "company", "location", "url", "description", mode="before")
    @classmethod
    def _strip(cls, value):
        """Trim, and treat whitespace-only as absent.

        A form field the student tabbed through holds " ", not None, and storing that produces
        a company named one space -- rendered as a blank line in the tracker rather than
        omitted. `title` has min_length=1 so this turns a whitespace-only title into the 422 it
        should be rather than a listing with no name.
        """
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class SaveJobRequest(BaseModel):
    listing_id: int
    status: SavedJobStatus = "saved"
    # Where the student was when they saved it. Client-supplied because only the client knows
    # which screen the tap came from -- and harmless if wrong, since it is a display label,
    # never an entitlement or an ownership claim. Recorded once and never rewritten.
    source: Literal["recommended", "saved_search", "job_alert", "manual"] = "manual"
    source_saved_search_id: Optional[int] = None


class UpdateSavedJobRequest(BaseModel):
    status: Optional[SavedJobStatus] = None
    notes: Optional[str] = Field(default=None, max_length=5000)
    follow_up_date: Optional[datetime.date] = None


class SavedJobRead(BaseModel):
    id: int
    status: SavedJobStatus
    # How this job reached the student, recorded at save time and never recomputed.
    source: Literal["recommended", "saved_search", "job_alert", "manual"] = "manual"
    source_saved_search_id: Optional[int] = None
    # Resolved name of that saved search, so the tracker can say "Saved Search: Dubai Data
    # Analyst" instead of an id. Null when the search has since been deleted -- the row keeps
    # its provenance, and the client falls back to the generic label.
    source_saved_search_name: Optional[str] = None
    notes: Optional[str] = None
    follow_up_date: Optional[datetime.date] = None
    applied_at: Optional[datetime.datetime] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    listing: JobListingRead
    # None when this student has no computed match for the listing yet -- a job saved before
    # any search of theirs scored it. Omitted rather than zero-filled.
    match: Optional[JobMatchRead] = None


# -- Saved searches -------------------------------------------------------------------------


class SavedSearchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    filters: JobSearchRequest
    notify_enabled: bool = False


class SavedSearchRead(BaseModel):
    id: int
    name: str
    # Typed rather than a bare dict so the client can re-run a saved search by replaying it
    # straight back into POST /job-hunt/search. Returning an untyped blob would have left the
    # app unable to do anything with a saved search except display its name. Stored filters
    # were themselves validated as a JobSearchRequest on the way in, so this round-trips.
    filters: JobSearchRequest = JobSearchRequest()
    notify_enabled: bool = False
    # Null means never run -- never zero, which would read as "we searched and found nothing".
    # last_error_at is separate so the client can distinguish a search that has gone quiet
    # because the market is quiet from one that has gone quiet because we could not ask.
    last_run_at: Optional[datetime.datetime] = None
    last_success_at: Optional[datetime.datetime] = None
    last_error_at: Optional[datetime.datetime] = None
    last_result_count: Optional[int] = None
    # True when this search has an alert attached, so the list can show it without a second
    # round trip.
    has_alert: bool = False
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


# -- Skill match analysis -------------------------------------------------------------------


class SkillMatchRowRead(BaseModel):
    """One row of the Skill Match Analysis screen.

    `status` is the provenance bucket (where the claim comes from) and `strength` is the badge
    (how it reads to a recruiter). Both are sent because they answer different questions and
    the client must not derive one from the other -- deriving would put the rule in two places
    and let them drift.
    """

    skill: str
    status: Literal["verified_evidence", "on_resume", "missing", "unclear"]
    strength: Literal["strong_match", "match", "skill_gap", "unclear"]
    evidence_state: Optional[
        Literal["claimed", "supported", "strong", "verified", "stale"]
    ] = None
    detail: Optional[str] = None


class SkillMatchAnalysisRead(BaseModel):
    total_score: float
    score_version: str
    rows: List[SkillMatchRowRead] = []
    verified_skill_count: int = 0
    # The single most improvable skill, or null when there is nothing actionable. Never
    # invented to fill the section.
    biggest_gap_skill: Optional[str] = None


# -- Application readiness ------------------------------------------------------------------


class ReadinessDimensionRead(BaseModel):
    key: Literal["skills", "experience", "resume", "education", "interview"]
    label: str
    # Null when this dimension could not be judged. Never 0.0 standing in for "unknown" --
    # the client renders null as "not enough information", not as an empty bar.
    score: Optional[float] = None
    detail: str


class ReadinessGapRead(BaseModel):
    kind: Literal["skill", "evidence", "dimension"]
    skill: Optional[str] = None
    title: str
    detail: str
    # Where "fix this" should send the student. The client maps these to its own routes.
    action: Literal["roadmap", "skill_passport", "resume", "interview", "education"]


class ApplicationReadinessRead(BaseModel):
    listing_id: int
    # Null when nothing at all could be judged.
    overall: Optional[float] = None
    band: Optional[
        Literal["strong_candidate", "worth_applying", "stretch", "prepare_first"]
    ] = None
    dimensions: List[ReadinessDimensionRead] = []
    # Named so the UI can say what it could not check, rather than silently showing four bars
    # where a student expects five.
    unknown_dimensions: List[str] = []
    biggest_gap: Optional[ReadinessGapRead] = None
    tips: List[str] = []


# -- Home summary ---------------------------------------------------------------------------


class JobHuntHomeRead(BaseModel):
    """Counts over this student's own rows. Never a claim about the job market as a whole."""

    new_jobs: int = 0
    best_matches: int = 0
    applied: int = 0
    saved: int = 0
    target_role_title: Optional[str] = None
    is_ready_to_search: bool = False
    # Top-ranked jobs already scored for this student, from what we last fetched. Empty before
    # the first search, which the client shows as an empty state rather than as "no jobs".
    recommended: List[JobResultRead] = []


# -- Job alerts -----------------------------------------------------------------------------


class JobAlertCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    saved_search_id: Optional[int] = None
    filters: Optional[JobSearchRequest] = None
    frequency: Literal["daily", "weekly"] = "daily"
    # 0 = Monday. Required for a weekly alert; the service rejects the combination rather than
    # silently picking a day the student never chose.
    day_of_week: Optional[int] = Field(default=None, ge=0, le=6)
    hour_local: int = Field(default=9, ge=0, le=23)
    quiet_hours_start: Optional[int] = Field(default=None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(default=None, ge=0, le=23)
    # Consent. Defaults to off: creating an alert and switching it on are two separate acts.
    is_enabled: bool = False


class JobAlertUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    frequency: Optional[Literal["daily", "weekly"]] = None
    day_of_week: Optional[int] = Field(default=None, ge=0, le=6)
    hour_local: Optional[int] = Field(default=None, ge=0, le=23)
    quiet_hours_start: Optional[int] = Field(default=None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(default=None, ge=0, le=23)
    is_enabled: Optional[bool] = None
    # Explicit, because None on the two fields above means "unchanged" and there would
    # otherwise be no way to remove a quiet window once set.
    clear_quiet_hours: bool = False


class JobAlertRead(BaseModel):
    id: int
    name: str
    saved_search_id: Optional[int] = None
    saved_search_name: Optional[str] = None
    filters: JobSearchRequest = JobSearchRequest()
    frequency: Literal["daily", "weekly"]
    day_of_week: Optional[int] = None
    hour_local: int
    quiet_hours_start: Optional[int] = None
    quiet_hours_end: Optional[int] = None
    is_enabled: bool
    # Three timestamps, not one: "never run", "ran and found nothing" and "ran and failed" are
    # three different things and the client says which.
    last_run_at: Optional[datetime.datetime] = None
    last_success_at: Optional[datetime.datetime] = None
    last_error_at: Optional[datetime.datetime] = None
    last_run_status: Optional[
        Literal[
            "success",
            "no_new_jobs",
            "provider_error",
            "not_configured",
            "skipped_quiet_hours",
            "requires_premium",
        ]
    ] = None
    # Null means never run. Never zero, which would read as "we looked and found nothing".
    last_new_job_count: Optional[int] = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class JobAlertHistoryRead(BaseModel):
    id: int
    alert_id: int
    alert_name: Optional[str] = None
    listing: JobListingRead
    notified_at: datetime.datetime


class JobAlertQuotaRead(BaseModel):
    """What the student's plan allows, so the client can explain a refusal rather than just
    disabling a button."""

    used: int
    limit: int
    is_premium: bool


class JobHuntEntitlementRead(BaseModel):
    """What the student is allowed to do next, decided server-side.

    Exists so the client can show the right screen -- "try one free search" before, an upgrade
    prompt after -- instead of discovering the answer by being refused. It is a UX aid and
    nothing more: every field is re-derived on the request that actually runs a search, so a
    client that ignores this, caches it, or edits it in memory changes nothing about what the
    server permits. See app/services/job_hunt/entitlement.py.
    """

    tier: Literal["free", "premium"]
    # True when an active subscription lifts the free-tier ceiling. Named for what it means to
    # the student rather than "is_premium", so the client renders an entitlement, not a plan.
    unlimited: bool
    free_uses_total: int
    free_uses_remaining: int
    has_used_free_search: bool
    # True only when the *next* search would be refused. A premium student who used their free
    # search long ago is not "requires_premium" -- they already have it.
    requires_premium: bool
    # When the free search was actually spent. None while it is still available, and None for
    # a student who has only ever searched with a subscription.
    last_free_use_at: Optional[datetime.datetime] = None


class JobHuntLimitDetail(BaseModel):
    """The body returned with 402 when a free student's one search is gone.

    Structured rather than a bare sentence because the client has to *act* on it (open the
    upgrade flow), and branching on prose is how a copy edit becomes a bug. `code` is the
    stable contract; `message` is what a person reads and may be reworded freely.
    """

    code: Literal["JOB_HUNT_FREE_LIMIT_REACHED"] = "JOB_HUNT_FREE_LIMIT_REACHED"
    message: str
    requires_premium: bool = True
