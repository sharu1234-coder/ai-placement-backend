from typing import List, Optional

from pydantic import BaseModel, model_validator


class PrepareMeRequest(BaseModel):
    role_id: Optional[int] = None
    jd_id: Optional[int] = None

    @model_validator(mode="after")
    def require_role_or_jd(self) -> "PrepareMeRequest":
        if self.role_id is None and self.jd_id is None:
            raise ValueError("Provide at least one of role_id or jd_id")
        return self


class BlueprintSection(BaseModel):
    skill: str
    assessment_type: str
    weight: float
    question_count: int
    is_gap: bool


class AssessmentBlueprintRead(BaseModel):
    id: int
    role_id: Optional[int]
    jd_id: Optional[int]
    source: str
    sections: List[BlueprintSection]
