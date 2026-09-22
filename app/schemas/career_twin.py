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

    readiness_percent: Optional[int] = None
    # Delta vs. the same role's snapshot from ~7 days ago. None until at least two real
    # snapshots exist -- never estimated or interpolated.
    readiness_trend_percent: Optional[int] = None
    is_estimate: bool = False
    reasons: List[str] = []

    skills: List[SkillTwinItemRead] = []
    biggest_gap_skill: Optional[str] = None
    next_action_hint: Optional[str] = None

    weak_skill_tags: List[str] = []

    # Evidence sources we could not read at all (evidence_engine.unavailable_sources), in
    # EvidenceItem.source vocabulary. Deliberately NOT premium-gated like `reasons` is: a
    # readiness number computed without a source the student never connected is a partial
    # measurement, and saying which part is missing is honesty about our own coverage rather
    # than an advanced insight to sell.
    unavailable_sources: List[str] = []


class CareerTwinReadinessRead(BaseModel):
    """A trimmed projection of CareerTwinRead for a cheap, cacheable Home-screen widget --
    everything except the per-skill list, which only the full Career Twin screen needs."""

    has_role: bool
    role_family_key: Optional[str] = None
    display_role_name: Optional[str] = None
    readiness_percent: Optional[int] = None
    readiness_trend_percent: Optional[int] = None
    is_estimate: bool = False
    reasons: List[str] = []
    biggest_gap_skill: Optional[str] = None
    next_action_hint: Optional[str] = None
    unavailable_sources: List[str] = []
