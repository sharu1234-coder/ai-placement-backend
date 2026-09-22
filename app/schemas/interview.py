import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class StartInterviewRequest(BaseModel):
    # "full_simulation" is the Full Recruiter Simulation: one continuous recruiter-style
    # conversation rather than a five-question probe of a single area. It is an ordinary
    # interview in every other respect -- same table, same endpoints, same evaluation -- so it
    # joins this Literal instead of getting a request model of its own. `interviews.type` is a
    # plain String(20) with no CHECK constraint, so this vocabulary is the only enforcement and
    # no migration is involved.
    type: Literal["technical", "hr", "project", "behavioral", "role_specific", "full_simulation"] = "technical"
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
    """The next question, plus the orchestration facts the client must not work out itself.

    The backend owns the stage plan and the question count, so it sends them. The client's job
    is to render them. Everything here is derived server-side from the interview's own type and
    the question's order_index -- none of it is accepted from the client, and none of it is
    asked of the AI.
    """

    completed: bool
    question: Optional[InterviewQuestionRead] = None
    # Which part of the recruiter conversation this question belongs to: INTRO, BACKGROUND,
    # RESUME, PROJECT, TECHNICAL, BEHAVIORAL, CANDIDATE_Q, or CLOSING once every question has
    # been answered. Null for the existing interview types, which genuinely have no stages.
    stage: Optional[str] = None
    # What to actually show a student. `stage` is an implementation detail and should not be
    # rendered -- "CANDIDATE_Q" is not a thing anyone says.
    stage_label: Optional[str] = None
    question_number: Optional[int] = None
    # Sent for every interview type. The Android screen used to hard-code 5 to render
    # "Question 2 of 5", which put a copy of a backend constant in the other repository; with
    # two possible lengths that copy would now be wrong half the time.
    total_questions: Optional[int] = None


class SubmitInterviewAnswerRequest(BaseModel):
    interview_question_id: int
    answer_text: str = Field(max_length=5_000)


class InterviewAnswerRead(BaseModel):
    interview_question_id: int
    answer_text: str


class InterviewEvaluationRead(BaseModel):
    """A completed interview's evaluation, which may or may not carry a grade.

    Every score is Optional because an interview whose AI review failed is deliberately left
    ungraded rather than given a placeholder. When `is_graded` is false every score here is
    null and `feedback` explains why; the client must render that as an un-graded state, never
    as a measured result and never as a low one. Those are different things and only one of
    them is about the student.
    """

    technical_correctness: Optional[float] = None
    relevance: Optional[float] = None
    completeness: Optional[float] = None
    reasoning: Optional[float] = None
    structure: Optional[float] = None
    clarity: Optional[float] = None
    grammar: Optional[float] = None
    speaking_pace: Optional[float] = None
    filler_word_rate: Optional[float] = None
    role_jd_coverage: Optional[float] = None
    overall_score: Optional[float] = None
    feedback: str
    # Derived from overall_score being set, not stored alongside it, so the flag and the scores
    # cannot disagree. Defaults true so a response from a backend predating this field reads as
    # graded -- which is the correct reading of every evaluation that existed before it.
    is_graded: bool = True
