import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel


class ProgressTrendPoint(BaseModel):
    created_at: datetime.datetime
    kind: Literal["assessment", "interview"]
    label: str
    score: float


class ProgressDashboardRead(BaseModel):
    readiness_score: Optional[float]
    completed_assessments: int
    completed_interviews: int
    roadmap_tasks_done: int
    roadmap_tasks_total: int
    top_weaknesses: List[str]
    trend: List[ProgressTrendPoint]
