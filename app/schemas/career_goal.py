import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.models.career_goal import CAREER_GOAL_STATUSES
from app.schemas.next_best_action import NextBestActionItem

# Mirrors ParseJDRequest.raw_text exactly, so a job description is refused at the same size
# whichever door it comes through. The ceiling is not cosmetic: the text is eventually
# interpolated into the JD-parsing prompt and sent to a provider with a fixed timeout, which
# makes an unbounded field a resource-exhaustion vector on this process and on the provider.
MAX_JOB_DESCRIPTION_LENGTH = 20_000

CareerGoalStatus = Literal[CAREER_GOAL_STATUSES]  # type: ignore[valid-type]


class CareerGoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=200)
    location: Optional[str] = Field(default=None, max_length=200)
    job_description: str = Field(min_length=1, max_length=MAX_JOB_DESCRIPTION_LENGTH)


class CareerGoalUpdate(BaseModel):
    """Every field optional: a PATCH that only moves the status must not require the client to
    echo back the whole job description. `None` therefore means "leave this alone" rather than
    "clear it" -- the one genuinely clearable field, location, is cleared by sending an empty
    string, which the service normalises to NULL."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    company: Optional[str] = Field(default=None, min_length=1, max_length=200)
    location: Optional[str] = Field(default=None, max_length=200)
    job_description: Optional[str] = Field(
        default=None, min_length=1, max_length=MAX_JOB_DESCRIPTION_LENGTH
    )
    status: Optional[CareerGoalStatus] = None


# ------------------------------------------------------------------------------------------
# Read models
# ------------------------------------------------------------------------------------------

# What a goal's preparation looks like as one word. Deterministic; see
# career_goal_service._preparation_state for the exact rule.
#
# not_analyzed is deliberately its own value rather than a flavour of not_started. "We have
# never read this job description" and "we have read it and you have not started" are
# different facts, and only the first one has an obvious button attached.
PREPARATION_STATES = ("not_analyzed", "not_started", "in_progress", "ready_for_review")


class CareerGoalListItem(BaseModel):
    id: int
    title: str
    company: str
    location: Optional[str] = None
    status: str
    is_analyzed: bool
    preparation_state: str  # see PREPARATION_STATES
    created_at: datetime.datetime
    updated_at: datetime.datetime


class GoalRequirementRead(BaseModel):
    """One thing the job description asks for, as the existing JD parser recorded it.

    `is_explicit` is carried through untouched and matters: false means the parser *inferred*
    this as commonly implied by the role rather than finding it in the text. Presenting the two
    identically would be exactly the fabrication this phase is meant to avoid.
    """

    category: str  # skill | tool | responsibility | experience | education
    value: str
    importance: str  # required | preferred
    is_explicit: bool


class GoalSkillEvidenceRead(BaseModel):
    """One JD skill, judged only by evidence this app has actually observed.

    Note what is absent: any statement about whether the candidate is qualified. This says what
    was found, never what it means about them.
    """

    skill: str
    importance: str  # required | preferred
    is_explicit: bool
    # claimed | supported | strong | verified | stale -- evidence_engine's vocabulary,
    # unchanged, so this and the Skill Passport can never disagree about the same skill.
    evidence_state: str
    evidence_band: str  # none | low | moderate | high | very_high
    # The three-way answer this phase is built around. Derived from evidence_state, never
    # stored, so it cannot drift from it. See career_goal_service._EVIDENCE_VERDICTS.
    verdict: str  # evidence_found | limited_evidence | no_evidence
    # Plain, factual wording for the client to show verbatim. Never "you are qualified".
    verdict_label: str
    evidence_count: int
    sources: List[str] = []
    last_verified_at: Optional[datetime.datetime] = None


class GoalInterviewRiskRead(BaseModel):
    """A preparation area, drawn only from real recorded signals -- a JD skill gap, a measured
    assessment result, or a graded interview dimension. Never inferred from nothing, and never
    a statement about the candidate's ability, character or fitness."""

    skill_tag: str
    label: str
    severity: str  # medium | high
    sources: List[str] = []
    # From Interview Memory (WeaknessRecord), when this app has a persisted cross-interview
    # record for the tag. None means no such record exists -- not zero occurrences.
    occurrences: Optional[int] = None
    memory_status: Optional[str] = None  # NEW | REPEATED | IMPROVING | NEARLY_CLOSED | CLOSED


class GoalPreparationItemRead(BaseModel):
    """One step of the preparation plan.

    Every item points at a feature that already exists; `destination` uses career_strategy's
    established vocabulary, which the Android app already resolves through one table. The plan
    is a fixed mapping from "what is missing" to "which existing feature addresses it", not a
    ranking -- ranking is next_best_action_service's job and is returned separately.
    """

    key: str
    title: str
    detail: str
    destination: str
    skill: Optional[str] = None


class GoalProgressRead(BaseModel):
    """Deterministic, bounded, and honest about what it does not know.

    Nothing here is AI-generated or estimated. Both percentages are NULL rather than 0 when
    there is nothing to measure, which is the same rule Phase 2A-0 applied to Career Path
    readiness and Phase 2C applied to an ungraded evaluation: a zero is a measurement, and
    showing one for an absence is a lie the student cannot detect.
    """

    # (JD skills with at least one real, non-self-asserted piece of evidence) / (JD skills),
    # as a whole percent. NULL when the goal has not been analyzed or the parse found no
    # matchable skills.
    evidence_coverage_percent: Optional[int] = None
    skills_total: int = 0
    skills_with_evidence: int = 0
    # Interviews this profile completed AFTER this goal was created. A count, so 0 is a real
    # measurement rather than an absence, and it stays a count rather than becoming a score.
    interviews_since_created: int = 0
    open_risks: int = 0
    state: str  # see PREPARATION_STATES


class CareerGoalDetailRead(BaseModel):
    """Everything the detail screen needs, computed on read from the engines that already own
    each part. No AI runs here -- see career_goal_service.get_detail."""

    id: int
    title: str
    company: str
    location: Optional[str] = None
    job_description: str
    status: str
    is_analyzed: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime

    # NULL until the job description has been analyzed at least once. The client renders the
    # "Analyze this job" state from is_analyzed rather than from an empty list, because
    # "never analyzed" and "analyzed and found nothing" need different words.
    analyzed_at: Optional[datetime.datetime] = None
    detected_role_title: Optional[str] = None

    requirements: List[GoalRequirementRead] = []
    aligned: List[GoalSkillEvidenceRead] = []
    needs_attention: List[GoalSkillEvidenceRead] = []
    unknown: List[GoalSkillEvidenceRead] = []
    interview_risks: List[GoalInterviewRiskRead] = []
    preparation_plan: List[GoalPreparationItemRead] = []
    progress: GoalProgressRead
    # The existing, profile-level recommendation, returned unchanged. Deliberately not a
    # job-scoped re-ranking: this app has exactly one recommendation engine and Phase 2D does
    # not add a second. NULL when the engine has nothing to recommend.
    next_best_action: Optional[NextBestActionItem] = None
    # Named, not merely absent: "no evidence for this skill" and "we never looked at the one
    # place your evidence lives" are different statements. See evidence_engine.
    unavailable_evidence_sources: List[str] = []


class CareerGoalAnalyzeResponse(BaseModel):
    """The result of running (or re-running) the job description parse."""

    id: int
    is_analyzed: bool
    detected_role_title: Optional[str] = None
    requirements_found: int = 0
    skills_found: int = 0
