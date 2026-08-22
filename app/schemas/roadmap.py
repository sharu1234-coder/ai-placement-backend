from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel


class GenerateRoadmapRequest(BaseModel):
    jd_id: Optional[int] = None
    duration_days: Literal[7, 14, 30, 90] = 30


class UpdateRoadmapTaskRequest(BaseModel):
    status: Literal["pending", "done", "skipped"]


class RoadmapTaskRead(BaseModel):
    id: int
    day_number: int
    task_type: str
    skill_tag: str
    title: str
    description: str
    status: str
    due_date: date


class RoadmapRead(BaseModel):
    id: int
    jd_id: Optional[int]
    duration_days: int
    status: str
    tasks: List[RoadmapTaskRead]
