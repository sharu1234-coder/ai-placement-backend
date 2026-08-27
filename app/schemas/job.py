import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ParseJDRequest(BaseModel):
    # Upper bound matches the resume upload's spirit (resumes.py caps at 10MB) -- without
    # one, an arbitrarily large raw_text gets embedded directly into the JD-parsing prompt
    # and sent to the AI provider with a fixed 120s timeout, a real resource-exhaustion
    # vector on both this process and the AI provider.
    raw_text: str = Field(min_length=1, max_length=20_000)


class RequirementRead(BaseModel):
    id: int
    category: str
    value: str
    is_explicit: bool
    importance: str


class JobDescriptionParseResponse(BaseModel):
    id: int
    role_title: str
    detected_role_id: Optional[int]
    location: Optional[str]
    employment_type: Optional[str]
    parsing_confidence: float
    requirements: List[RequirementRead]


class JobDescriptionListItem(BaseModel):
    """One row of GET /jobs -- a past JD analysis, for the Android app's 'reopen a saved
    analysis' list. has_preparation_plan tells the client whether tapping it can jump
    straight to a plan or needs to generate one first."""

    id: int
    role_title: str
    location: Optional[str]
    created_at: datetime.datetime
    has_preparation_plan: bool
    preparation_plan_company_name: Optional[str]
    preparation_plan_job_title: Optional[str]


class SkillGapRead(BaseModel):
    id: int
    skill_name: str
    gap_severity: str
    source: str


class JobMatchRead(BaseModel):
    id: int
    matching_skills: List[str]
    missing_skills: List[str]
    match_score: float
    skill_gaps: List[SkillGapRead]


class GeneratePreparationPlanRequest(BaseModel):
    # Optional labels the student typed in on the Analyzer screen -- distinct from
    # role_title, which is the AI's own best-guess job title from the JD text itself.
    company_name: Optional[str] = Field(default=None, max_length=255)
    job_title: Optional[str] = Field(default=None, max_length=255)


class JobSummaryRead(BaseModel):
    company_name: Optional[str]
    job_title: str
    experience_expectation: Optional[str]
    education_eligibility: Optional[str]
    location: Optional[str]
    employment_type: Optional[str]


class RequiredSkillRead(BaseModel):
    category: str  # "skill" | "tool"
    value: str
    importance: str  # "required" | "preferred"


class SkillGapStatusRead(BaseModel):
    skill: str
    status: Literal["strong", "needs_improvement", "missing"]


class PreparationPlanCategoryRead(BaseModel):
    category: str
    reason: str


class InterviewPrepQuestionRead(BaseModel):
    bucket: str
    question: str


class JobPreparationPlanRead(BaseModel):
    id: int
    jd_id: int
    company_name: Optional[str]
    job_title: Optional[str]
    job_summary: JobSummaryRead
    required_skills: List[RequiredSkillRead]
    preparation_gaps: List[SkillGapStatusRead]
    preparation_plan: List[PreparationPlanCategoryRead]
    interview_prep: List[InterviewPrepQuestionRead]
    created_at: datetime.datetime
