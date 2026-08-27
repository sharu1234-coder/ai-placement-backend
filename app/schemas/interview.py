import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class StartInterviewRequest(BaseModel):
    type: Literal["technical", "hr", "project", "behavioral", "role_specific"] = "technical"
    mode: Literal["text", "voice"] = "text"
    jd_id: Optional[int] = None


class InterviewRead(BaseModel):
    id: int
    type: str
    mode: str
    jd_id: Optional[int]
    status: str


class InterviewHistoryItem(BaseModel):
    """One past interview, for the Mock Interviews 'Previous Interviews' list.
    overall_score is None until InterviewEvaluation exists (get_evaluation computes and
    caches it, same lazy-eval pattern as AssessmentResult)."""

    id: int
    type: str
    mode: str
    status: str
    overall_score: Optional[float]
    created_at: datetime.datetime


class InterviewQuestionRead(BaseModel):
    id: int
    order_index: int
    question_text: str
    based_on: str


class NextInterviewQuestionResponse(BaseModel):
    completed: bool
    question: Optional[InterviewQuestionRead] = None


class SubmitInterviewAnswerRequest(BaseModel):
    interview_question_id: int
    answer_text: str = Field(max_length=5_000)


class InterviewAnswerRead(BaseModel):
    interview_question_id: int
    answer_text: str


class InterviewEvaluationRead(BaseModel):
    technical_correctness: float
    relevance: float
    completeness: float
    reasoning: float
    structure: float
    clarity: float
    grammar: Optional[float]
    speaking_pace: Optional[float]
    filler_word_rate: Optional[float]
    role_jd_coverage: float
    overall_score: float
    feedback: str
