"""unified voice conversation history

Voice Career Coach conversations were never stored. `MemoryStore` is `dict[str, SessionMemory]` --
bounded, per-process session memory, held so follow-up turns on one socket share context. It was
never a record, and it is not one: restart the server and every coaching conversation a student
ever had is gone.

Mock interviews were better served but still incomplete. `interview_questions`,
`interview_answers` and `interview_evaluations` are real tables, so an interview's content and
grading survive. What did not exist was a single place a student could open to read back what was
said, across both kinds of session, in order.

This revision adds that place.

`conversations` is one row per voice session, keyed on the server-minted `session_key` so a
reconnect that presents the same session id appends to the transcript it already started instead
of opening a parallel one. `conversation_turns` is one row per thing said, ordered by `seq`.

**It is a transcript, not a second source of truth for grading.** An interview's rubric, scores
and evaluations stay in the interview tables; `conversations.interview_id` points at them.
Copying evaluation here would create two answers to "what did this student score", which is
exactly the ambiguity the `operation_claims` table exists elsewhere in this schema to prevent.

The constraint that earns its place is `uq_conversation_turn_identity`: UNIQUE (conversation_id,
turn_id, role). A realtime socket retries -- a student loses signal mid-answer, the client
reconnects and re-sends the same committed turn -- and without this the transcript grows a
duplicate of what they said. `LS §13.2`: "a repeated mutation cannot create a duplicate state when
idempotency semantics require one result." It is scoped per role because one turn legitimately
produces two rows: the student's words, and the agent's reply.

**No audio column, deliberately.** Retaining a recording of a student discussing their weaknesses
needs its own disclosure, consent, retention policy and deletion path, none of which exists. The
history requirement is met by text. Leaving the column out means that decision cannot be taken by
accident later.

Nothing existing is altered, dropped or backfilled. Both tables are new, so this revision is
additive and its downgrade is a clean drop.

Revision ID: d4a1c08f52e7
Revises: c9e2f5a71b04
"""

import sqlalchemy as sa
from alembic import op

revision = "d4a1c08f52e7"
down_revision = "c9e2f5a71b04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("session_key", sa.String(length=64), nullable=False),
        sa.Column("interview_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=20), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["profile_id"], ["student_profiles.id"]),
        sa.ForeignKeyConstraint(["interview_id"], ["interviews.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_profile_id", "conversations", ["profile_id"])
    op.create_index("ix_conversations_kind", "conversations", ["kind"])
    op.create_index("ix_conversations_status", "conversations", ["status"])
    op.create_index("ix_conversations_interview_id", "conversations", ["interview_id"])
    # Unique: the session key is how a reconnect finds its own transcript.
    op.create_index(
        "ix_conversations_session_key", "conversations", ["session_key"], unique=True
    )
    # The history list is always "this student's conversations, newest first".
    op.create_index(
        "ix_conversations_owner_recent", "conversations", ["profile_id", "updated_at"]
    )

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("interview_question_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["interview_question_id"], ["interview_questions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "turn_id", "role", name="uq_conversation_turn_identity"
        ),
    )
    op.create_index(
        "ix_conversation_turns_conversation_id", "conversation_turns", ["conversation_id"]
    )
    op.create_index(
        "ix_conversation_turns_interview_question_id",
        "conversation_turns",
        ["interview_question_id"],
    )
    op.create_index(
        "ix_conversation_turns_ordered", "conversation_turns", ["conversation_id", "seq"]
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_turns_ordered", table_name="conversation_turns")
    op.drop_index(
        "ix_conversation_turns_interview_question_id", table_name="conversation_turns"
    )
    op.drop_index("ix_conversation_turns_conversation_id", table_name="conversation_turns")
    op.drop_table("conversation_turns")

    op.drop_index("ix_conversations_owner_recent", table_name="conversations")
    op.drop_index("ix_conversations_session_key", table_name="conversations")
    op.drop_index("ix_conversations_interview_id", table_name="conversations")
    op.drop_index("ix_conversations_status", table_name="conversations")
    op.drop_index("ix_conversations_kind", table_name="conversations")
    op.drop_index("ix_conversations_profile_id", table_name="conversations")
    op.drop_table("conversations")
