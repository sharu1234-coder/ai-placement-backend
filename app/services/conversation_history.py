"""Append-as-you-go persistence for voice transcripts, and owner-scoped reads of them.

## The two properties that matter

**Incremental.** A turn is saved when it completes, not when the session ends. A student who
loses signal after eight answers keeps eight answers. Saving only at the end would mean the record
exists precisely when it is least needed.

**Idempotent.** A realtime socket retries, so the same committed turn can arrive twice. Every
write here is keyed on the protocol's own server-minted `turn_id`, and a replay is a no-op that
returns the row already stored rather than a second one. `LS §13.2`: "a repeated mutation cannot
create a duplicate state when idempotency semantics require one result."

## Why `seq` is safe to compute as `max + 1`

That read-then-write would race if two turns could be appended to one conversation concurrently.
They cannot: the voice gateway already refuses a second commit while a turn is in flight
(`refused a commit: turn_in_flight`), so a conversation is written by one turn at a time. The
uniqueness constraint is still the thing relied on for correctness -- `seq` is for ordering, and a
replay cannot reach the insert at all.

## Ownership

Every read and delete takes `profile_id` and filters on it in the query. Not "fetch then compare":
a filtered query cannot be refactored into a leak by someone who forgets the check afterwards, and
a row belonging to another student is simply not found.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_KINDS,
    CONVERSATION_STATUSES,
    TURN_ROLES,
    Conversation,
    ConversationTurn,
)

logger = logging.getLogger(__name__)

#: A defensive cap on one page of history. The client may ask for less, never more.
MAX_PAGE = 100


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def ensure_conversation(
    db: Session,
    *,
    profile_id: int,
    kind: str,
    session_key: str,
    interview_id: Optional[int] = None,
    mode: Optional[str] = None,
    title: Optional[str] = None,
) -> Conversation:
    """The conversation for `session_key`, creating it only if this session is new.

    Keyed on the session id rather than on "most recent conversation for this student", so a
    reconnect presenting the same session id appends to the transcript it already started.
    """
    if kind not in CONVERSATION_KINDS:
        raise ValueError("unknown conversation kind: " + repr(kind))

    existing = db.execute(
        select(Conversation).where(Conversation.session_key == session_key)
    ).scalar_one_or_none()
    if existing is not None:
        # A session key is server-minted and unique, so a mismatch here would mean one student's
        # session id was presented by another. Refuse rather than append to someone else's record.
        if existing.profile_id != profile_id:
            raise PermissionError("session key does not belong to this profile")
        return existing

    conversation = Conversation(
        profile_id=profile_id,
        kind=kind,
        session_key=session_key,
        interview_id=interview_id,
        mode=mode,
        title=title,
        status="active",
    )
    db.add(conversation)
    try:
        # A savepoint, so losing the race to another connection does not poison the surrounding
        # transaction -- the same shape `_insert` uses for operation claims.
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        db.rollback()
        # The winner's row is now committed; this session reads it and appends to it.
        return db.execute(
            select(Conversation).where(Conversation.session_key == session_key)
        ).scalar_one()
    return conversation


def append_turn(
    db: Session,
    *,
    conversation: Conversation,
    role: str,
    text: str,
    turn_id: str,
    interview_question_id: Optional[int] = None,
) -> Optional[ConversationTurn]:
    """Store one thing said. Returns `None` when this exact turn was already stored.

    `None` rather than raising: a replay is normal operation on a realtime socket, not an error,
    and the caller's only reasonable response is to carry on.
    """
    if role not in TURN_ROLES:
        raise ValueError("unknown turn role: " + repr(role))
    if not text or not text.strip():
        # Nothing was said. Storing an empty row would put a turn in the transcript that the
        # student never produced, and would be counted by the final report.
        return None

    next_seq = (
        db.execute(
            select(func.coalesce(func.max(ConversationTurn.seq), 0)).where(
                ConversationTurn.conversation_id == conversation.id
            )
        ).scalar_one()
        + 1
    )

    turn = ConversationTurn(
        conversation_id=conversation.id,
        seq=next_seq,
        role=role,
        text=text,
        turn_id=turn_id,
        interview_question_id=interview_question_id,
    )
    db.add(turn)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        db.rollback()
        # The uniqueness constraint fired: this turn and role are already recorded.
        return None

    conversation.updated_at = _now()
    db.flush()
    return turn


def mark_ended(db: Session, *, conversation: Conversation, status: str) -> None:
    """Close a conversation.

    `interrupted` is a real outcome and must not be rewritten to `completed`: a dropped connection
    is not a finished interview, and the student's own record should not claim it was.
    """
    if status not in CONVERSATION_STATUSES:
        raise ValueError("unknown conversation status: " + repr(status))
    conversation.status = status
    conversation.ended_at = _now()
    conversation.updated_at = _now()
    db.flush()


def list_conversations(
    db: Session,
    *,
    profile_id: int,
    kind: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> Sequence[Conversation]:
    """This student's conversations, newest activity first."""
    if kind is not None and kind not in CONVERSATION_KINDS:
        raise ValueError("unknown conversation kind: " + repr(kind))

    query = select(Conversation).where(Conversation.profile_id == profile_id)
    if kind is not None:
        query = query.where(Conversation.kind == kind)
    # `id` as a tiebreak so a page boundary is stable when two conversations share a timestamp;
    # without it the same row can appear on two pages.
    query = query.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
    query = query.limit(max(1, min(limit, MAX_PAGE))).offset(max(0, offset))
    return db.execute(query).scalars().all()


def get_conversation(
    db: Session, *, profile_id: int, conversation_id: int
) -> Optional[Conversation]:
    """One conversation, or `None` if it does not exist **or** is not this student's."""
    return db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.profile_id == profile_id
        )
    ).scalar_one_or_none()


def get_turns(
    db: Session, *, conversation_id: int, limit: int = 500, offset: int = 0
) -> Sequence[ConversationTurn]:
    """A conversation's turns in order.

    Takes a conversation id that the caller has already proved ownership of via
    [get_conversation]; it does not re-derive ownership, so it must never be called with an id
    straight from a request.
    """
    query = (
        select(ConversationTurn)
        .where(ConversationTurn.conversation_id == conversation_id)
        .order_by(ConversationTurn.seq.asc())
        .limit(max(1, min(limit, 1000)))
        .offset(max(0, offset))
    )
    return db.execute(query).scalars().all()


def delete_conversation(db: Session, *, profile_id: int, conversation_id: int) -> bool:
    """Delete one conversation and its turns. `False` when there was nothing of theirs to delete.

    The turns go first: there is no database-level cascade on this foreign key, and deleting the
    parent alone would leave rows pointing at a conversation that no longer exists.
    """
    conversation = get_conversation(db, profile_id=profile_id, conversation_id=conversation_id)
    if conversation is None:
        return False

    db.execute(
        delete(ConversationTurn).where(ConversationTurn.conversation_id == conversation.id)
    )
    db.delete(conversation)
    db.flush()
    return True
