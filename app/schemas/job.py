from typing import List, Optional

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
    parsing_confidence: float
    requirements: List[RequirementRead]


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
