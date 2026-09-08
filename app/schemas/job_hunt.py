"""Request/response shapes for Job Hunt.

Literal types rather than bare `str` on every enumerated field: FastAPI then rejects an
unknown work mode or pipeline status with a 422 before any handler runs, which keeps the
service layer free of defensive re-validation and means an invalid status can never reach the
database.
"""

import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

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
    url: str
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


class SaveJobRequest(BaseModel):
    listing_id: int
    status: SavedJobStatus = "saved"


class UpdateSavedJobRequest(BaseModel):
    status: Optional[SavedJobStatus] = None
    notes: Optional[str] = Field(default=None, max_length=5000)
    follow_up_date: Optional[datetime.date] = None


class SavedJobRead(BaseModel):
    id: int
    status: SavedJobStatus
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
    last_run_at: Optional[datetime.datetime] = None
    last_result_count: Optional[int] = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}
