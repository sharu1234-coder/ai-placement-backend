"""Wire shapes for Career Strategy.

Two things about this file are load-bearing.

**There is no hiring-probability field, and there is nowhere to put one.** Not "we don't
populate it" -- the field does not exist on any model here, so no future change can start
leaking one by accident. The same goes for any single collapsed "how good are you" number:
measured score, evidence confidence and interview performance stay separate all the way to the
client, because merging them is exactly how a resume claim starts reading as a demonstrated
skill.

**Premium fields are Optional/empty by default so the free projection can genuinely drop
them.** `career_strategy_service.project` returns a response with those fields absent rather
than a full response the client is trusted to hide. A free client that inspects its own traffic
finds nothing gated in it.
"""

import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# The sections a free client will find empty, named so the UI can show an upgrade prompt
# instead of an empty state. These are labels for the paywall -- they carry no gated content.
LOCKABLE_SECTIONS = (
    "why",
    "evidence",
    "queue",
    "impact_effort",
    "contradictions",
    "diminishing_returns",
    "do_not_do_yet",
    "what_changed",
)


class BottleneckRead(BaseModel):
    """The single constraint most worth removing.

    The first block is the free preview: what it is, and one thing to do about it. Everything
    below `description` is Premium and is omitted -- not blanked -- for free clients.
    """

    id: str
    category: str
    subject: str
    subject_kind: str
    title: str
    destination: str
    recommended_action: str

    description: Optional[str] = None
    severity: Optional[str] = None
    impact: Optional[str] = None
    effort: Optional[str] = None
    # A band, never a percentage. See bottleneck.Bottleneck.confidence.
    confidence: Optional[str] = None
    evidence: List[str] = []
    affected_roles: List[str] = []
    affected_job_ids: List[int] = []
    reason_code: Optional[str] = None
    created_from_signals: List[str] = []
    decision_signals: List[str] = []
    strategy_version: int


class QueueItemRead(BaseModel):
    id: str
    category: str
    subject: str
    title: str
    impact: str
    effort: str
    destination: str
    reason: str
    status: str


class DeferredItemRead(BaseModel):
    id: str
    # None when this is a strength rather than a gap -- a skill already mastered produces no
    # gap at all, which is precisely why saying "you don't need more of this" is useful.
    category: Optional[str] = None
    subject: str
    title: str
    reason: str
    instead: Optional[str] = None


class ContradictionRead(BaseModel):
    kind: str
    # None for contradictions about the profile as a whole rather than about one skill -- a
    # rich resume scoring badly on ATS is not a fact about any single subject.
    subject: Optional[str] = None
    left_statement: str
    left_signal_keys: List[str] = []
    right_statement: str
    right_signal_keys: List[str] = []
    explanation: str
    # Advisory only. The category this subject would be better treated as, when the
    # contradiction implies the obvious reading is wrong. Surfaced rather than applied: silently
    # rewriting a gap's category would make the gap list disagree with the reason shown next
    # to it.
    reclassify_to: Optional[str] = None


class PerformanceSeriesRead(BaseModel):
    subject: str
    verdict: str
    observation_count: int
    is_sufficient: bool
    # All None below MIN_OBSERVATIONS. A mean of one result is that result, and presenting it
    # as a trend would be the fabrication this whole layer exists to prevent.
    mean: Optional[float] = None
    variance: Optional[float] = None
    trend: Optional[float] = None
    latest: Optional[float] = None
    reason: str


class ChangedItemRead(BaseModel):
    id: str
    category: str
    subject: str
    title: str


class StrategyChangeRead(BaseModel):
    changed: bool
    from_sequence: Optional[int] = None
    to_sequence: Optional[int] = None
    from_created_at: Optional[datetime.datetime] = None
    to_created_at: Optional[datetime.datetime] = None

    state_from: Optional[str] = None
    state_to: Optional[str] = None
    state_changed: bool = False

    bottleneck_changed: bool = False
    bottleneck_from: Optional[ChangedItemRead] = None
    bottleneck_to: Optional[ChangedItemRead] = None

    cleared: List[ChangedItemRead] = []
    displaced: List[ChangedItemRead] = []
    added: List[ChangedItemRead] = []
    still_open: List[ChangedItemRead] = []

    spans_version_change: bool = False
    version_from: Optional[int] = None
    version_to: Optional[int] = None

    summary: List[str] = []


class SnapshotSummaryRead(BaseModel):
    id: int
    sequence: int
    state: str
    strategy_version: int
    primary_bottleneck_id: Optional[str] = None
    primary_category: Optional[str] = None
    primary_subject: Optional[str] = None
    created_at: datetime.datetime
    # What changed at this point, relative to the snapshot immediately before it. Filled in on
    # the history listing so a timeline can be rendered from one call; empty on the very oldest
    # entry ever recorded, which has nothing behind it to compare against. Left empty rather
    # than filled with a plausible line: an invented "you improved here" is exactly the kind of
    # flattering fiction this feature refuses to produce.
    change_summary: List[str] = []


class StrategyHistoryRead(BaseModel):
    snapshots: List[SnapshotSummaryRead] = []


class StrategyComparisonRead(BaseModel):
    snapshot: SnapshotSummaryRead
    compared_to: Optional[SnapshotSummaryRead] = None
    change: StrategyChangeRead


class CareerStrategyRead(BaseModel):
    """The whole strategy screen in one response.

    `is_premium` and `locked_sections` exist so the client can render an upgrade prompt for a
    section instead of an empty state; the gated content itself is simply not in the payload.
    """

    is_premium: bool
    locked_sections: List[str] = []

    state: str
    state_reason: str
    strategy_version: int

    has_role: bool
    role_family_key: Optional[str] = None
    display_role_name: Optional[str] = None
    role_source: str = "none"
    # True when the role was inferred rather than chosen. Carried through from role_context so
    # the screen can say "we think you're targeting X" rather than asserting it.
    role_is_estimate: bool = False

    # Career Twin's EVIDENCE COVERAGE number, not a second calculation. None when it has no
    # basis. Renamed from readiness_percent in Phase 2G along with its two companions below,
    # because all three describe the same metric and splitting their names would leave the
    # screen reading "evidence coverage" from a field called readiness.
    evidence_coverage_percent: Optional[int] = None
    evidence_coverage_is_estimate: bool = False
    # Career Twin's own explanation of that number, carried through verbatim. Free, because the
    # /career-twin endpoint it comes from is free -- gating it here would only hide from this
    # screen what the same student can already read on another one, which is not a paywall, just
    # an inconsistency. A coverage figure with no working shown is a number to distrust.
    evidence_coverage_reasons: List[str] = []

    # How much was actually visible. Drives the honest empty state: "3 more things to record"
    # rather than a confident-looking strategy built on one assessment.
    signal_count: int = 0

    bottleneck: Optional[BottleneckRead] = None

    queue: List[QueueItemRead] = []
    do_not_do_yet: List[DeferredItemRead] = []
    contradictions: List[ContradictionRead] = []
    diminishing_returns: List[PerformanceSeriesRead] = []
    latest_change: Optional[StrategyChangeRead] = None

    snapshot_id: Optional[int] = None
    snapshot_sequence: Optional[int] = None
    snapshot_created_at: Optional[datetime.datetime] = None
    # True when this read is what caused a new snapshot to be written -- i.e. the strategy
    # actually moved since last time. False on the common path.
    snapshot_is_new: bool = False


class JobConditionsRead(BaseModel):
    """The advert's own terms.

    Salary is four fields, not one formatted string, and that is the point: the period travels
    with the amount so no client can render 500/hour as 500/year. A None period means the
    provider did not say, and is shown as undisclosed rather than assumed to be annual.
    """

    location: Optional[str] = None
    work_mode: Optional[str] = None
    employment_type: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: Optional[str] = None
    salary_period: Optional[str] = None
    # True when the provider estimated the figure rather than the employer publishing it.
    salary_is_predicted: bool = False


class ReadinessBiggestGapRead(BaseModel):
    """job_hunt/readiness's own "one thing most worth doing before applying".

    Typed rather than passed through as a free-form map so a shape change in readiness.py
    breaks here, loudly, instead of reaching a client as a silently empty card. `skill` is None
    for a dimension-level gap, which genuinely has no single skill behind it.
    """

    kind: str
    skill: Optional[str] = None
    title: str
    detail: str
    action: str


class JobApplicationStateRead(BaseModel):
    """What the student already did about this advert.

    `applied_at` is authoritative for "did they apply" -- a rejected application is still an
    application. `status` is carried through uninterpreted because the schema does not document
    what its values mean; a client that renders it should render it as-is.
    """

    is_saved: bool = False
    applied_at: Optional[datetime.datetime] = None
    status: Optional[str] = None


class JobBlockerRead(BaseModel):
    kind: str
    subject: str
    title: str
    detail: str
    destination: str


class JobStrategyRead(BaseModel):
    listing_id: int
    job_title: str
    company: Optional[str] = None

    # APPLY_NOW | FIX_BEFORE_APPLYING | NOT_A_CURRENT_PRIORITY | INSUFFICIENT_INFORMATION.
    # Never a probability, and never a recommendation not to apply -- NOT_A_CURRENT_PRIORITY
    # says this advert is off the student's critical path, not that they should stay away.
    decision: str
    headline: str
    reasons: List[str] = []

    match_score: Optional[float] = None
    score_version: Optional[str] = None
    # The match engine's own breakdown and its one-sentence explanation, read from the stored
    # JobListingMatch row. Present so this screen cannot drift from the Job Hunt card.
    component_scores: Dict[str, float] = {}
    match_rationale: Optional[str] = None
    unknown_factors: List[str] = []

    # The advert's terms, straight from the listing. Salary keeps its period as a separate
    # field so a client physically cannot render an hourly rate as an annual one.
    conditions: JobConditionsRead = JobConditionsRead()
    # The match components that decided this, in sentences. Restates stored numbers; adds none.
    deciding_factors: List[str] = []
    application: JobApplicationStateRead = JobApplicationStateRead()

    readiness_percent: Optional[int] = None
    readiness_band: Optional[str] = None
    judged_dimensions: int = 0
    unknown_dimensions: List[str] = []
    # job_hunt/readiness's own most-improvable-gap object, carried through rather than
    # recomputed. None when readiness found nothing worth naming -- never a filler task.
    biggest_gap: Optional[ReadinessBiggestGapRead] = None

    verified_skill_count: int = 0
    missing_skills: List[str] = []
    unproven_skills: List[str] = []
    # Skills the advert asks for that we cannot judge at all, because we hold no skill list for
    # this student. Distinct from missing_skills: "we don't know" is not "you don't have it".
    unclear_skills: List[str] = []

    blockers: List[JobBlockerRead] = []
    prep_from_career_strategy: List[str] = []


# -- multi-target ------------------------------------------------------------------------------


class SetStrategyTargetsRequest(BaseModel):
    """Replace the saved comparison set. An empty list clears it, which is a real request --
    "stop comparing" needs to be expressible."""

    role_ids: List[int] = Field(default_factory=list, max_length=3)


class TargetStrategyRead(BaseModel):
    """One target role, judged by the same engine that judges the primary one.

    Every field here is produced by an existing service: evidence coverage and effort by
    career_decision_service, the bottleneck and action by the Career Strategy engine with the
    role context swapped. Nothing in this shape is computed specially for the comparison.
    """

    # None when this target is a Career Path rather than a catalogue Role -- Career Paths have
    # their own family keys and no Role row. Null rather than a sentinel like -1: this whole
    # feature refuses magic numbers for unknowns, and a client that treated -1 as an id would
    # produce a broken link rather than an obvious absence.
    role_id: Optional[int] = None
    display_role_name: str
    # True for the student's actual target. The others are hypotheticals, and the screen must
    # be able to say which is which -- a comparison that hides your real target among options
    # invites switching by accident.
    is_current_target: bool = False

    # career_decision_service's evidence_coverage_percent, identical to what Career Decision
    # Lab shows for this role. None only if it could not be computed at all -- never a zero
    # standing in for one. Renamed from readiness_percent in Phase 2G to match its source.
    evidence_coverage_percent: Optional[int] = None
    classification: Optional[str] = None  # best_fit | alternative | stretch
    # Always an estimate, and labelled as one. Never a probability of being hired.
    estimated_hours_to_strengthen: Optional[float] = None
    is_estimate: bool = True

    state: str
    bottleneck_title: Optional[str] = None
    bottleneck_category: Optional[str] = None
    top_action: Optional[str] = None
    top_action_destination: Optional[str] = None
    impact: Optional[str] = None
    effort: Optional[str] = None

    # Gaps that exist for this target and no other in the set -- the actual cost of choosing it.
    unique_gaps: List[str] = []
    # Gaps shared with at least one other target: work that is not wasted whichever way the
    # student goes.
    shared_gaps: List[str] = []


class TargetComparisonRead(BaseModel):
    is_premium: bool
    # Empty for a free client: the comparison itself is Premium. `locked` says so, so the
    # screen shows an upgrade prompt rather than an empty table.
    locked: bool = False
    targets: List[TargetStrategyRead] = []
    # Skills required by two or more of the compared roles, from career_decision_service.
    shared_skills: List[str] = []
    # Plain-language trade-offs, each derived from the numbers above rather than written to
    # sound insightful. Empty when there is nothing honest to say.
    tradeoffs: List[str] = []
    # career_decision_service's own stated assumptions, carried through verbatim so an effort
    # estimate is never shown without them.
    assumptions: List[str] = []
    max_targets: int
