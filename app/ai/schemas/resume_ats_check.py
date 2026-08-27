from typing import List

from pydantic import BaseModel, Field


class GeneratedFindingRephrase(BaseModel):
    finding_key: str
    rationale: str = Field(max_length=300)
    how_to_improve: str = Field(max_length=300)


class GeneratedAtsCheckContent(BaseModel):
    summary: str = Field(max_length=600)
    finding_rephrases: List[GeneratedFindingRephrase]
