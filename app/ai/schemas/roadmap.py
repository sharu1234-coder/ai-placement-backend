from typing import List, Literal

from pydantic import BaseModel


class GeneratedRoadmapTask(BaseModel):
    day: int
    task_type: Literal["learn", "practice", "test", "interview", "reassessment"]
    skill_tag: str
    title: str
    description: str


class GeneratedRoadmap(BaseModel):
    """duration_days is a prompt input (known server-side), not requested here -- same
    pattern as GeneratedQuestion excluding skill_tag/difficulty."""

    tasks: List[GeneratedRoadmapTask]
