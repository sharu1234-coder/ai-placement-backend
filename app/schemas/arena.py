import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ArenaChallengeRead(BaseModel):
    key: str
    title: str
    category: str
    description: str
    question_count: int
    time_limit_seconds: int
    # None until the student's first completed attempt at this challenge -- never a fabricated
    # zero (matches the codebase's "no fake data" empty-state rule).
    personal_best_score: Optional[float] = None


class ArenaAttemptStartRead(BaseModel):
    arena_attempt_id: int
    assessment_id: int
    challenge_key: str
    time_limit_seconds: int


class CompleteArenaAttemptRequest(BaseModel):
    # Purely informational/display ("you finished in Xs") -- never used to compute the score
    # or to decide timed_out (both are server-authoritative, see arena_service.complete_attempt).
    time_taken_seconds: int = Field(ge=0)


class ArenaAttemptResultRead(BaseModel):
    arena_attempt_id: int
    score: float
    timed_out: bool
    is_personal_best: bool
    previous_best_score: Optional[float] = None


class ArenaAttemptHistoryItem(BaseModel):
    arena_attempt_id: int
    score: float
    time_taken_seconds: Optional[int]
    timed_out: bool
    is_personal_best: bool
    completed_at: datetime.datetime


class ArenaChallengeHistoryRead(BaseModel):
    challenge_key: str
    personal_best_score: Optional[float] = None
    attempts: List[ArenaAttemptHistoryItem]
