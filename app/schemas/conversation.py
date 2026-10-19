"""Read models for the unified voice conversation history.

Read-only by design. A transcript is written by the voice gateway as the conversation happens;
there is no request body here that lets a client author a turn, because a client-authored
transcript would be a record of something the recogniser never produced.
"""

import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

#: Mirrors `app.models.conversation.CONVERSATION_KINDS`. A `Literal` rather than `str`, so an
#: unknown filter is a 422 from FastAPI instead of reaching the service layer.
ConversationKind = Literal["career_coach", "mock_interview"]


class ConversationTurnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    role: str
    text: str
    interview_question_id: Optional[int] = None
    created_at: datetime.datetime


class ConversationRead(BaseModel):
    """One row in the history list. Deliberately without turns: the list is a list."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    #: `active`, `completed` or `interrupted`. The third is not an error state -- it is how a
    #: conversation that lost its connection is honestly described.
    status: str
    mode: Optional[str] = None
    title: Optional[str] = None
    interview_id: Optional[int] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    ended_at: Optional[datetime.datetime] = None


class ConversationDetail(ConversationRead):
    """One conversation with its turns, in order."""

    turns: list[ConversationTurnRead]
