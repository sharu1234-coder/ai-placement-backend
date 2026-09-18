from typing import List, Optional

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


class GeneratedGapGuidance(BaseModel):
    """Prose for ONE skill gap. `skill` must echo a skill the prompt supplied; the service
    drops any entry whose skill it did not ask about, so the model cannot introduce a gap the
    deterministic matching engine never found."""

    skill: str
    why_it_matters: str = Field(max_length=300)
    recommended_action: str = Field(max_length=300)
    practice_direction: str = Field(max_length=300)


class GeneratedResumeRecommendation(BaseModel):
    """One suggested resume change. `current_fact` must restate something the prompt showed it
    from the student's own resume -- it is the model's own claim about what the resume says, so
    the service only keeps recommendations whose area it recognises, and never persists any of
    this as a resume fact of record."""

    area: str
    requirement: Optional[str] = None
    current_fact: str = Field(max_length=400)
    suggestion: str = Field(max_length=600)
    rationale: str = Field(max_length=300)


class GeneratedProjectConnection(BaseModel):
    """Why one of the student's real projects matters for one JD requirement. Both
    `project_title` and `requirement` must echo values the prompt supplied; anything else is
    discarded, which is what stops a plausible-sounding but non-existent project from reaching
    the student."""

    project_title: str
    requirement: str
    why_relevant: str = Field(max_length=400)
    likely_questions: List[str] = Field(default_factory=list)


class GeneratedJobPreparationContent(BaseModel):
    category_reasons: List[GeneratedCategoryReason]
    interview_questions: List[GeneratedInterviewQuestion]
    # Defaulted, not required: a provider that returns the older three-field shape (or omits a
    # section it had nothing grounded to say about) must still produce a usable plan rather
    # than failing validation and losing the category reasons and questions along with it.
    gap_guidance: List[GeneratedGapGuidance] = Field(default_factory=list)
    resume_recommendations: List[GeneratedResumeRecommendation] = Field(default_factory=list)
    project_connections: List[GeneratedProjectConnection] = Field(default_factory=list)
