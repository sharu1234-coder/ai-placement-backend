import datetime
from typing import List, Optional

from pydantic import BaseModel


class MissionRead(BaseModel):
    id: int
    mission_date: datetime.date
    tier: Optional[str] = None
    title: str
    action_type: str  # learn | practice | project | resume | roadmap | maintenance
    estimated_minutes: Optional[int] = None
    reasons: List[str] = []
    skill: Optional[str] = None
    status: str  # not_started | in_progress | completed
    started_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None
    assessment_id: Optional[int] = None

    model_config = {"from_attributes": True}


class CompleteMissionRequest(BaseModel):
    # The real Assessment the student actually completed to close this mission out, if any --
    # optional because some action_types (e.g. "roadmap", "resume") have no assessment to point
    # to; completion is never gated on providing one.
    assessment_id: Optional[int] = None
