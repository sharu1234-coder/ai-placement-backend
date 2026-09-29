import datetime
from typing import List, Optional

from pydantic import BaseModel


class SkillTwinItemRead(BaseModel):
    skill: str
    weight: float
    # 0-100, taken directly from the strongest scored evidence item (an assessment or
    # interview score). None when the only evidence is unscored (a project, a resume mention)
    # -- never fabricated to fill the field.
    measured_score: Optional[float] = None
    # 0-100, but only ever one of five values (0/30/45/70/90) -- one per state. Kept for
    # ordering and for bar lengths; do NOT render it as a precise percentage, because the
    # engine has no such resolution. Show confidence_band instead.
    confidence_percent: int
    confidence_band: str  # none | low | moderate | high | very_high
    state: str  # claimed | supported | strong | verified | stale
    # An actual verification date: set only by evidence the student did not self-assert.
    last_verified_at: Optional[datetime.datetime] = None


class CareerTwinRead(BaseModel):
    has_role: bool
    role_family_key: Optional[str] = None
    display_role_name: Optional[str] = None
    role_source: str  # career_path | target_role | none

    # EVIDENCE COVERAGE: the weighted mean of evidence confidence across this role's required
    # skills. Renamed from readiness_percent in Phase 2G -- readiness in this product means
    # JobReadinessRead.overall_percent, a five-signal average, and one word must not name two
    # formulas. The formula behind this number is unchanged.
    #
    # None when the role defines no skills to weigh: the calculation cannot run at all, which
    # is a different fact from "we measured you and found nothing". Never defaults to 0.
    evidence_coverage_percent: Optional[int] = None
    # Delta vs. the same role's snapshot from ~7 days ago. None until at least two real
    # snapshots exist -- never estimated or interpolated.
    evidence_coverage_trend_percent: Optional[int] = None
    is_estimate: bool = False
    reasons: List[str] = []

    skills: List[SkillTwinItemRead] = []
    biggest_gap_skill: Optional[str] = None
    next_action_hint: Optional[str] = None

    # What has actually happened to this student's applications, as counts.
    #
    # These sit BESIDE evidence_coverage_percent and do not feed it. Coverage is a
    # statement about evidence for role skills; an application outcome is a statement about one
    # employer's decision, which turns on budget, timing, visas and internal candidates that no
    # code here can see. Folding a rejection into a skill-evidence number would invent a causal
    # link this product cannot justify -- so the number is left alone and the facts are
    # reported next to it. See app/services/application_outcome.py.
    applications_total: int = 0
    applications_live: int = 0
    applications_at_interview_stage: int = 0
    application_offers: int = 0
    application_rejections: int = 0

    weak_skill_tags: List[str] = []

    # Evidence sources we could not read at all (evidence_engine.unavailable_sources), in
    # EvidenceItem.source vocabulary. Deliberately NOT premium-gated like `reasons` is: a
    # coverage number computed without a source the student never connected is a partial
    # measurement, and saying which part is missing is honesty about our own coverage rather
    # than an advanced insight to sell.
    unavailable_sources: List[str] = []


class CareerTwinReadinessRead(BaseModel):
    """A trimmed projection of CareerTwinRead for a cheap, cacheable Home-screen widget --
    everything except the per-skill list, which only the full Career Twin screen needs.

    The class keeps its name because it names the GET /career-twin/readiness endpoint whose
    payload it is, not the metric it carries -- renaming the class without the route would be
    a worse inconsistency than either, and the route is a URL contract outside Phase 2G's
    scope. Its fields carry the Phase 2G names, which is where the honesty actually matters."""

    has_role: bool
    role_family_key: Optional[str] = None
    display_role_name: Optional[str] = None
    evidence_coverage_percent: Optional[int] = None
    evidence_coverage_trend_percent: Optional[int] = None
    is_estimate: bool = False
    reasons: List[str] = []
    biggest_gap_skill: Optional[str] = None
    next_action_hint: Optional[str] = None
    unavailable_sources: List[str] = []
