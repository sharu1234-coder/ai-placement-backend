from typing import List, Optional

from pydantic import BaseModel


class ParseJDRequest(BaseModel):
    raw_text: str


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
