from typing import List

from pydantic import BaseModel, Field


class GeneratedCategoryReason(BaseModel):
    """One preparation category's "why it matters for this JD" text. category must echo
    one of the category names the prompt supplied verbatim -- the service layer validates
    this and never trusts an unrecognized/missing one (falls back to a deterministic
    template sentence instead, same discipline as placement_path_service's explanations)."""

    category: str
    reason: str = Field(max_length=300)


class GeneratedInterviewQuestion(BaseModel):
    # bucket must echo one of: technical, coding, sql, system_design, hr, company_specific
    bucket: str
    question: str = Field(max_length=400)


class GeneratedJobPreparationContent(BaseModel):
    category_reasons: List[GeneratedCategoryReason]
    interview_questions: List[GeneratedInterviewQuestion]
