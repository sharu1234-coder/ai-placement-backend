import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class PrepareMeRequest(BaseModel):
    role_id: Optional[int] = None
    jd_id: Optional[int] = None
    # Narrows the blueprint to one assessment_type (e.g. "sql", "coding") -- powers the
    # Test Center's category browse. None keeps today's mixed, multi-section behavior.
    category: Optional[str] = None

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


class StartAssessmentRequest(BaseModel):
    blueprint_id: int


class AssessmentRead(BaseModel):
    id: int
    blueprint_id: int
    status: str


class QuestionRead(BaseModel):
    """Deliberately excludes expected_answer_or_rubric and explanation -- those must not
    reach the client until after the student answers (see AnswerResult)."""

    id: int
    skill_tag: str
    type: str
    difficulty: str
    prompt: str
    options: Optional[List[str]]


class NextQuestionResponse(BaseModel):
    completed: bool
    question: Optional[QuestionRead] = None


class SubmitAnswerRequest(BaseModel):
    question_id: int
    student_answer: str = Field(max_length=5_000)
    time_taken_seconds: Optional[int] = Field(default=None, ge=0)


class AnswerResult(BaseModel):
    question_id: int
    is_correct: bool
    score: float
    explanation: str


class SkillScore(BaseModel):
    skill_tag: str
    score: float


class AssessmentResultRead(BaseModel):
    assessment_id: int
    overall_score: float
    skill_scores: List[SkillScore]
    priority_weaknesses: List[str]


class AssessmentHistoryItem(BaseModel):
    """One completed test, for the Test Center's stats + recent-results list. category is
    the blueprint's dominant (highest-weight) section's assessment_type -- a mixed blueprint
    still gets one representative label. accuracy is correct/answered from QuestionAttempt,
    distinct from overall_score (the AI-graded average, which can be partial credit)."""

    assessment_id: int
    blueprint_id: int
    category: str
    overall_score: float
    accuracy: float
    question_count: int
    completed_at: datetime.datetime
