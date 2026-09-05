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
    """Every score is Optional because a session whose AI evaluation failed is deliberately
    left ungraded rather than given a placeholder number -- see recruiter_attack_service's
    _fallback_evaluation. When used_fallback is true, every score here is null and `feedback`
    carries the "we couldn't grade this, try again" text; the client must render that as an
    un-graded state, never as a measured result."""

    technical_defense: Optional[float] = None
    project_ownership: Optional[float] = None
    specificity: Optional[float] = None
    communication: Optional[float] = None
    overall_score: Optional[float] = None
    feedback: str
    # True when the AI evaluation could not be produced and this response carries no real
    # grade. Defaults to false so a genuinely graded session reads exactly as before.
    used_fallback: bool = False
