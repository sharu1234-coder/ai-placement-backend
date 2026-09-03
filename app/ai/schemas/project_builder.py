from typing import List

from pydantic import BaseModel, Field


class GeneratedStageGuidance(BaseModel):
    """One stage's guidance text. stage_key must echo one of the 10 fixed stage keys the
    prompt supplied verbatim -- the service layer validates this and never trusts an
    unrecognized or missing key (falls back to a deterministic template sentence instead),
    same "never trust an unrecognized key" enforcement used by career_path's explainer."""

    stage_key: str
    guidance_text: str = Field(max_length=500)


class GeneratedStageGuidanceSet(BaseModel):
    stages: List[GeneratedStageGuidance]


class GeneratedProjectProof(BaseModel):
    resume_bullet: str = Field(max_length=300)
    readme_markdown: str = Field(max_length=3000)
    explanation_60s: str = Field(max_length=600)
    explanation_5min: str = Field(max_length=2500)
    defense_questions: List[str]
