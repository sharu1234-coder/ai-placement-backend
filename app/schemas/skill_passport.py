import datetime
from typing import List, Optional

from pydantic import BaseModel


class EvidenceItemRead(BaseModel):
    source: str  # assessment | practice | project | interview | resume
    source_id: Optional[int] = None
    label: str
    score: Optional[float] = None
    detected_at: Optional[datetime.datetime] = None


class SkillPassportItemRead(BaseModel):
    skill: str
    # None when state == "claimed" -- with zero real evidence there's nothing to base a
    # proficiency level on, so it's omitted rather than defaulted to "Beginner".
    level: Optional[str] = None
    confidence_percent: int
    state: str  # claimed | supported | strong | verified | stale
    last_verified_at: Optional[datetime.datetime] = None
    evidence: List[EvidenceItemRead] = []
    next_action_hint: str
