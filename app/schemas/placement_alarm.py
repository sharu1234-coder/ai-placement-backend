from typing import List, Optional

from pydantic import BaseModel, Field


class StartChallengeRequest(BaseModel):
    client_alarm_id: str = Field(min_length=1, max_length=64)


class AlarmEligibilityRead(BaseModel):
    eligible: bool


class ChallengeQuestionRead(BaseModel):
    """Deliberately excludes the answer/explanation -- same withholding rule as
    assessments' QuestionRead, until the student actually answers."""

    id: int
    index: int
    skill_tag: str
    prompt: str
    options: List[str]


class ChallengeStateRead(BaseModel):
    challenge_id: int
    status: str
    completed_count: int
    current_question: Optional[ChallengeQuestionRead] = None


class SubmitChallengeAnswerRequest(BaseModel):
    question_id: int
    student_answer: str = Field(max_length=2_000)


class ChallengeAnswerResult(BaseModel):
    """correct_answer/explanation are populated only when is_correct is False -- the
    confirmed product decision is to reveal the answer on a wrong attempt (so the student
    isn't stuck unable to silence the alarm on something they don't know), but not to repeat
    it on a correct answer, where the student already knows they got it right."""

    question_id: int
    is_correct: bool
    correct_answer: Optional[str] = None
    explanation: Optional[str] = None
    completed_count: int
    challenge_completed: bool
