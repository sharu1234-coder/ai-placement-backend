"""One durable home for everything said in a voice session, coach or interview.

## Why this exists

`MemoryStore` is `dict[str, SessionMemory]`. It is bounded, per-process session memory and it was
never a record: restart the server and every Voice Career Coach conversation is gone. Mock
interviews fared better -- `interview_questions`, `interview_answers` and `interview_evaluations`
are real tables -- but there was no single place a student could open to read back what was said,
and nothing at all persisted the coach side.

These two tables are that place. They are deliberately *not* a replacement for the interview
tables: an interview's rubric, score and evaluation stay where they already live, and
[Conversation.interview_id] points at them. This is the transcript, not a second source of truth
for grading. Duplicating evaluation here would create two answers to "what did the student score",
which is the problem `operation_claims` exists to avoid elsewhere in this schema.

## Why a turn carries `turn_id`

A realtime socket retries. A student loses signal mid-answer, the client reconnects and re-sends
the same committed turn, and without an idempotency key the transcript grows a duplicate -- which
is exactly what `LS §13.2` forbids ("a repeated mutation cannot create a duplicate state when
idempotency semantics require one result"). `turn_id` is the protocol's own server-minted turn
identifier, so the uniqueness constraint below makes a replayed turn a no-op rather than a second
row. It is scoped per `(conversation, turn, role)` because one turn legitimately produces two
rows: what the student said, and what the agent replied.

## What is deliberately not stored

**Raw microphone audio.** Retaining a recording of a student discussing their weaknesses needs its
own disclosure, consent, retention policy and deletion path. None of that exists yet, and
"conversation history" does not require it -- the text does. There is no audio column here, so the
decision cannot be made accidentally by a later `setattr`.
"""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


#: The two kinds of session that produce a transcript. A closed vocabulary rather than free text,
#: so the history filter cannot be asked for a kind that no writer produces.
CONVERSATION_KINDS = ("career_coach", "mock_interview")

#: `active` while the socket is open; `completed` when it ended cleanly; `interrupted` when it did
#: not. The third value is the honest one: a dropped connection is not a completed interview, and
#: showing it as one would misrepresent the student's own record back to them.
CONVERSATION_STATUSES = ("active", "completed", "interrupted")

TURN_ROLES = ("user", "assistant")


class Conversation(Base):
    """One voice session's transcript, owned by exactly one profile."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The same owner column the interview tables use, so ownership checks are one pattern.
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )
    #: One of [CONVERSATION_KINDS].
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    #: The server-minted session id the socket already mints. Unique, so a reconnect that presents
    #: the same session id resumes this transcript instead of starting a parallel one.
    session_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    #: Set for `mock_interview`. The rubric, scores and evaluations stay in the interview tables;
    #: this is the link to them, not a copy of them.
    interview_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("interviews.id"), nullable=True, index=True
    )
    #: Practice & Coach vs Realistic Interview, mirroring `interviews.mode`. Nullable because a
    #: coach conversation has no interview mode.
    mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=_now
    )
    #: Bumped on every appended turn, so the history list can order by recency without counting
    #: rows in the child table.
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=_now
    )
    ended_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # The history list is always "this student's conversations, newest first", optionally
        # filtered by kind. One composite index serves both shapes.
        Index("ix_conversations_owner_recent", "profile_id", "updated_at"),
    )


class ConversationTurn(Base):
    """One thing said, by one side, at one position in a conversation."""

    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    #: Monotonic within a conversation. Explicit rather than relying on `id` order, because `id`
    #: is global and a reader must not depend on two conversations' rows never interleaving.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: One of [TURN_ROLES].
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: The protocol's server-minted turn id. See the module docstring on why this is the thing
    #: that makes a reconnect safe.
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Set on an assistant turn in a mock interview, linking the spoken reply to the question it
    #: answered. Nullable everywhere else.
    interview_question_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("interview_questions.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=_now
    )

    __table_args__ = (
        # The constraint that makes a replayed turn a no-op rather than a duplicate row. Scoped
        # per role because one turn produces both a user row and an assistant row.
        UniqueConstraint(
            "conversation_id", "turn_id", "role", name="uq_conversation_turn_identity"
        ),
        # Reading a transcript is always "this conversation, in order".
        Index("ix_conversation_turns_ordered", "conversation_id", "seq"),
    )
