import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class SelectCareerRoleRequest(BaseModel):
    """Exactly one of role_id/custom_role must be set -- a catalog pick or a free-text role
    the student typed in, never both, never neither. Mirrors RoleSelectRequest's exact
    validator shape (app/schemas/profile.py) -- deliberately a separate type, since Career
    Path's role selection never touches StudentProfile.target_role_id/custom_target_role."""

    role_id: Optional[int] = None
    custom_role: Optional[str] = Field(default=None, max_length=150)

    @model_validator(mode="after")
    def _exactly_one(self) -> "SelectCareerRoleRequest":
        # A whitespace-only custom_role (e.g. "   ") would otherwise pass this check, then
        # strip down to an empty role name at generation time -- wasting an AI call on a
        # nonsensical prompt instead of failing cleanly with a 422 here.
        normalized_custom_role = self.custom_role.strip() if self.custom_role is not None else None
        if not normalized_custom_role:
            normalized_custom_role = None
        if (self.role_id is None) == (normalized_custom_role is None):
            raise ValueError("Provide exactly one of role_id or a non-blank custom_role")
        self.custom_role = normalized_custom_role
        return self


class UpdateSkillItemRequest(BaseModel):
    status: str = Field(pattern="^(in_progress|completed)$")


class UpdateProjectRequest(BaseModel):
    status: str = Field(pattern="^(not_started|in_progress|completed)$")


class RoleCatalogEntryRead(BaseModel):
    family_key: str
    display_name: str
    category: str
    short_explanation: str


class RoleOverviewRead(BaseModel):
    display_name: str
    category: str
    short_explanation: str
    responsibilities: List[str]
    core_skills: List[str]
    tools: List[str]
    difficulty: str
    entry_level_expectations: str
    interview_focus: List[str]
    portfolio_expectations: str


class SkillItemRead(BaseModel):
    id: int
    phase_key: str
    skill_key: str
    title: str
    level: str
    why_it_matters: Optional[str]
    order_index: int
    status: str
    completed_via: Optional[str]
    has_practice: bool

    model_config = {"from_attributes": True}


class CareerPathProjectRead(BaseModel):
    id: int
    project_key: str
    title: str
    difficulty: str
    skills_demonstrated: List[str]
    technologies: List[str]
    problem_statement: Optional[str]
    expected_outcome: Optional[str]
    what_should_be_included: Optional[str]
    why_recruiters_value: Optional[str]
    resume_bullet_suggestion: Optional[str]
    interview_questions: List[str]
    order_index: int
    status: str

    model_config = {"from_attributes": True}


class CareerPathPhaseRead(BaseModel):
    phase_key: str
    title: str
    objective: Optional[str]
    order_index: int
    status: str
    skill_items: List[SkillItemRead]
    projects: List[CareerPathProjectRead]


class SkillGapItemRead(BaseModel):
    skill: str
    status: str  # strong | needs_improvement | missing | recommended
    evidence: Optional[str] = None


class ResumeConnectionRead(BaseModel):
    skills_demonstrated: int
    skills_total: int
    projects_demonstrated: int
    projects_total: int
    role_keyword_coverage_percent: int
    missing_evidence: List[str]


class InterviewPrepCategorySummaryRead(BaseModel):
    category: str
    questions_completed: int
    accuracy_percent: Optional[int]
    weak_areas: List[str]


class JobReadinessRead(BaseModel):
    overall_percent: int
    skills_percent: int
    projects_percent: int
    resume_percent: int
    practice_percent: int
    interview_percent: int
    top_focus_areas: List[str]
    is_estimate: bool = False


class CareerPathSummaryRead(BaseModel):
    id: int
    role_family_key: str
    display_role_name: str
    is_primary: bool
    readiness_percent: int
    completed_items: int
    total_items: int
    generated_via: str
    created_at: datetime.datetime
    updated_at: datetime.datetime


class CareerPathRead(BaseModel):
    id: int
    role_id: Optional[int]
    custom_role: Optional[str]
    role_family_key: str
    display_role_name: str
    is_primary: bool
    generated_via: str
    role_overview: RoleOverviewRead
    phases: List[CareerPathPhaseRead]
    skill_gaps: List[SkillGapItemRead]
    resume_connection: Optional[ResumeConnectionRead]
    interview_prep: List[InterviewPrepCategorySummaryRead]
    job_readiness: JobReadinessRead
    created_at: datetime.datetime
    updated_at: datetime.datetime


class PracticeHandoffRead(BaseModel):
    blueprint_id: int
