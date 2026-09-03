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
    confidence_percent: int  # 0-100, evidence_engine's confidence rounded for display
    state: str  # claimed | supported | strong | verified | stale
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
