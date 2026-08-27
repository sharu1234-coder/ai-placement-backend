import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class CreateAtsCheckRequest(BaseModel):
    target_role: Optional[str] = Field(default=None, max_length=255)


class CategoryBreakdownRead(BaseModel):
    category: str
    score: int
    summary: str
    strengths: List[str]
    improvements: List[str]
    how_to_improve: List[str]


class SectionAnalysisRead(BaseModel):
    section: str
    status: str
    content: Optional[str]


class IssueRead(BaseModel):
    category: str
    severity: str
    title: str
    description: str
    how_to_fix: str


class RecommendationRead(BaseModel):
    rank: int
    category: str
    title: str
    priority: str
    rationale: str
    fix_target: Optional[str]


class AtsCheckReportRead(BaseModel):
    id: int
    resume_id: int
    target_role: Optional[str]
    overall_score: int
    score_category: str
    category_breakdown: List[CategoryBreakdownRead]
    section_analysis: List[SectionAnalysisRead]
    issues: List[IssueRead]
    recommendations: List[RecommendationRead]
    potential_score: Optional[int]
    potential_score_explanation: Optional[str]
    ai_summary: str
    raw_text: str
    created_at: datetime.datetime


class AtsCheckListItemRead(BaseModel):
    id: int
    resume_id: int
    resume_filename: Optional[str]
    overall_score: int
    score_category: str
    created_at: datetime.datetime
