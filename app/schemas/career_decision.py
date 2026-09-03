from typing import List, Optional

from pydantic import BaseModel, Field


class SimulateCareerDecisionsRequest(BaseModel):
    role_ids: List[int] = Field(min_length=2, max_length=3)
    weekly_hours: Optional[int] = None


class CareerDecisionRoleRead(BaseModel):
    role_id: int
    display_role_name: str
    readiness_percent: int
    classification: str  # best_fit | alternative | stretch
    biggest_gap_skill: Optional[str] = None
    # Always an estimate -- see career_decision_service's module docstring. Never a
    # probability-of-employment number; no such field exists anywhere in this schema.
    estimated_hours_to_strengthen: float
    is_estimate: bool = True
    estimated_weeks: Optional[int] = None
    reasons: List[str]


class CareerDecisionSimulationRead(BaseModel):
    roles: List[CareerDecisionRoleRead]
    shared_skills: List[str]
    weekly_hours_used: Optional[int] = None
    assumptions: List[str]
