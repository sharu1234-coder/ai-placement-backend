import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class StartRecruiterAttackSessionRequest(BaseModel):
    source_type: Literal["project_build", "resume_claim"]
    project_build_id: Optional[int] = None
    # Must be a real substring of the student's own uploaded resume -- see
    # recruiter_attack_service.start_session. Never a freeform, unverified claim.
    claim_text: Optional[str] = Field(default=None, max_length=300)


class RecruiterAttackTurnRead(BaseModel):
    id: int
    order_index: int
    question_text: str
    based_on: str
    answer_text: Optional[str] = None


class RecruiterAttackSessionRead(BaseModel):
    id: int
    source_type: str
    source_label: str
    status: str  # in_progress | completed
    created_at: datetime.datetime
    completed_at: Optional[datetime.datetime] = None


class RecruiterAttackSessionListItem(RecruiterAttackSessionRead):
    overall_score: Optional[float] = None


class NextRecruiterAttackTurnResponse(BaseModel):
    completed: bool
    turn: Optional[RecruiterAttackTurnRead] = None


class SubmitRecruiterAttackAnswerRequest(BaseModel):
    turn_id: int
    answer_text: str = Field(max_length=5_000)


class RecruiterAttackResultsRead(BaseModel):
    technical_defense: float
    project_ownership: float
    specificity: float
    communication: float
    overall_score: float
    feedback: str
