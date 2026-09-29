import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel


class ProgressTrendPoint(BaseModel):
    created_at: datetime.datetime
    kind: Literal["assessment", "interview"]
    label: str
    score: float


class ProgressDashboardRead(BaseModel):
    # PERFORMANCE: the mean of the latest assessment's and the latest interview's overall
    # score, on a 0-1 scale. Renamed from readiness_score in Phase 2F -- it measures how the
    # student PERFORMED on the last two things they actually did, which is neither evidence
    # coverage (Career Twin) nor preparation progress (Career Path) nor the five-signal
    # readiness in JobReadinessRead. One word must not name four formulas.
    #
    # Already correctly Optional, and stays that way: None means no assessment and no
    # interview have been completed, which is not a performance of zero.
    performance_score: Optional[float]
    completed_assessments: int
    completed_interviews: int
    roadmap_tasks_done: int
    roadmap_tasks_total: int
    top_weaknesses: List[str]
    trend: List[ProgressTrendPoint]
