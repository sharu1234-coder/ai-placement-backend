import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel


class PlacementPathTopicRead(BaseModel):
    id: int
    stage: str
    track_key: str
    topic_key: str
    title: str
    why_it_matters: str
    skill_tag: str
    status: str  # locked/not_started/in_progress/completed -- the derived, display-facing value
    priority: str  # high/medium/low -- computed live, never stored
    prerequisite_topic_id: Optional[int] = None
    has_practice: bool
    completed_via: Optional[str] = None


class PlacementPathStageRead(BaseModel):
    stage: str
    topics: List[PlacementPathTopicRead]


class NextBestActionRead(BaseModel):
    topic_id: int
    title: str
    reason: str


class PlacementPathRead(BaseModel):
    id: int
    status: str
    created_at: datetime.datetime
    completed_topics: int
    total_topics: int
    stages: List[PlacementPathStageRead]
    next_best_action: Optional[NextBestActionRead] = None


class PracticeHandoffRead(BaseModel):
    """blueprint_id is all the Android client needs -- it hands off entirely to the existing,
    unmodified assessment flow (POST /assessments {blueprint_id} -> adaptive question loop)."""

    blueprint_id: int


class UpdatePlacementPathTopicRequest(BaseModel):
    """This endpoint's only purpose is the honest manual-completion fallback for topics that
    have no single skill to practice (the 3 checkpoint stages) -- everything else is either
    derived (locked/priority) or driven by /start and /practice."""

    status: Literal["completed"]
