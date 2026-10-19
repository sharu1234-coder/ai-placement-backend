"""One authenticated place to read back everything said in a voice session.

Voice Career Coach and Mock Interviews both write to the same two tables, so this is one endpoint
group rather than two: the history list a student opens is genuinely unified, not two lists shown
side by side.

Every route derives the owning profile from the access token and passes it into the query. No
route takes a profile or user id from the request, so there is no parameter a client could change
to read someone else's conversation -- a row belonging to another student is simply not found,
which is a 404 rather than a 403 so the endpoint cannot be used to confirm that an id exists.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.conversation import (
    ConversationDetail,
    ConversationKind,
    ConversationRead,
    ConversationTurnRead,
)
from app.services import conversation_history
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationRead])
def list_conversations(
    kind: ConversationKind | None = Query(
        default=None, description="Limit to one kind of session."
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ConversationRead]:
    """This student's conversations, most recently active first."""
    profile = get_or_create_profile(db, current_user)
    rows = conversation_history.list_conversations(
        db, profile_id=profile.id, kind=kind, limit=limit, offset=offset
    )
    return [ConversationRead.model_validate(row) for row in rows]


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConversationDetail:
    """One conversation and its ordered transcript."""
    profile = get_or_create_profile(db, current_user)
    conversation = conversation_history.get_conversation(
        db, profile_id=profile.id, conversation_id=conversation_id
    )
    if conversation is None:
        # 404 for "not yours" as well as "not there": a 403 would confirm the id exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found."
        )

    turns = conversation_history.get_turns(db, conversation_id=conversation.id)
    detail = ConversationDetail.model_validate(
        {
            **ConversationRead.model_validate(conversation).model_dump(),
            "turns": [ConversationTurnRead.model_validate(turn) for turn in turns],
        }
    )
    return detail


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete one conversation and its turns.

    Irreversible, and intentionally so: this is the student's own record of what they said, and
    "delete my conversation" has to actually delete it. The turns go with it.
    """
    profile = get_or_create_profile(db, current_user)
    deleted = conversation_history.delete_conversation(
        db, profile_id=profile.id, conversation_id=conversation_id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found."
        )
    db.commit()
    # No body, and no transcript content in the log line.
    logger.info("deleted conversation %s", conversation_id)
