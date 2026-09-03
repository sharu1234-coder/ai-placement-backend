import datetime
from typing import List

from pydantic import BaseModel


class PendingNotificationRead(BaseModel):
    id: int
    kind: str  # mission_completed | weakness_improved | evidence_verified | readiness_milestone
    title: str
    body: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class AcknowledgeNotificationsRequest(BaseModel):
    ids: List[int]
