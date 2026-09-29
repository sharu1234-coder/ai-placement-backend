from typing import List, Optional

from pydantic import BaseModel, Field


class SimulateCareerDecisionsRequest(BaseModel):
    role_ids: List[int] = Field(min_length=2, max_length=3)
    weekly_hours: Optional[int] = None


class CareerDecisionRoleRead(BaseModel):
    role_id: int
    display_role_name: str
    # EVIDENCE COVERAGE: the weighted mean of evidence confidence across this role's required
    # skills -- the same quantity career_twin_service computes. Renamed from readiness_percent
    # in Phase 2F because readiness in this product means JobReadinessRead's five-signal
    # average, and one word must not name two formulas.
    #
    # None when the role has no defined skills: the comparison cannot be made at all. It used
    # to be 0, which asserted a measurement that never happened.
    evidence_coverage_percent: Optional[int] = None
    # None exactly when evidence_coverage_percent is None. A role we cannot measure cannot be
    # classified, and defaulting it to "stretch" turned missing configuration into a verdict
    # about the candidate.
    classification: Optional[str] = None  # best_fit | alternative | stretch
    biggest_gap_skill: Optional[str] = None
    # Always an estimate -- see career_decision_service's module docstring. Never a
    # probability-of-employment number; no such field exists anywhere in this schema.
    # None, not 0.0, for an unmeasurable role: "0 hours to close the gap" would read as
    # "you are already there" for a role nothing is known about.
    estimated_hours_to_strengthen: Optional[float] = None
    is_estimate: bool = True
    estimated_weeks: Optional[int] = None
    reasons: List[str]


class CareerDecisionSimulationRead(BaseModel):
    roles: List[CareerDecisionRoleRead]
    shared_skills: List[str]
    weekly_hours_used: Optional[int] = None
    assumptions: List[str]
