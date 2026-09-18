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


class GapGuidanceRead(BaseModel):
    """Grounded guidance for one skill the student is missing or weak on.

    skill, severity and is_explicit_requirement are DETERMINISTIC -- they come from the
    Requirement row and the existing matching engine, never from the AI. Only the three prose
    fields are generated, and only for a skill the deterministic layer already established as a
    gap, so the AI can explain a gap but can never invent one or promote an inferred
    requirement into an explicit one.
    """

    skill: str
    severity: str
    is_explicit_requirement: bool
    requirement_importance: str
    why_it_matters: str
    recommended_action: str
    practice_direction: str


class ResumeRecommendationItemRead(BaseModel):
    """One suggested resume change for this specific JD.

    current_fact is what the student's resume actually says today, and that is now enforced
    rather than merely requested of the model: jd_grounding.ground_resume_recommendations
    drops any item whose current_fact contradicts a deterministic skill gap, matches no known
    skill/project/flagged section, carries a metric the resume does not support, or cites a
    requirement this JD never stated. An item that cannot be grounded is dropped, never
    repaired -- the only honest alternative to an unverifiable fact is no fact.

    "Not present in your resume" is the one exempt value: it asserts absence, which cannot
    overstate the student.

    suggestion is wording built only from that fact plus the JD's own language. Nothing here
    edits the uploaded PDF -- these are recommendations the student chooses to act on.
    """

    area: str
    requirement: Optional[str]
    current_fact: str
    suggestion: str
    rationale: str


class ResumeRecommendationsRead(BaseModel):
    """Recommendations plus an explicit reason when there are none.

    status carries the "why" so the client never has to guess whether an empty list means
    "your resume already covers this JD" or "we have no resume to look at".
    """

    status: Literal["ready", "no_resume", "insufficient_resume_data"]
    missing_keywords: List[str]
    items: List[ResumeRecommendationItemRead]


class ProjectConnectionRead(BaseModel):
    """A JD requirement tied to a project the student genuinely has.

    project_title and requirement are matched deterministically against the student's own
    project records; the AI only writes why_relevant and the questions. A requirement with no
    real matching project produces no connection at all rather than an invented one.
    """

    requirement: str
    project_title: str
    project_source: Literal["resume", "profile"]
    why_relevant: str
    likely_questions: List[str]


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
    # Added after the first release of this endpoint. Optional so that plans saved before the
    # columns existed still deserialise -- an older row reads back as None, not as an error.
    gap_guidance: Optional[List[GapGuidanceRead]] = None
    resume_recommendations: Optional[ResumeRecommendationsRead] = None
    project_connections: Optional[List[ProjectConnectionRead]] = None
    created_at: datetime.datetime
