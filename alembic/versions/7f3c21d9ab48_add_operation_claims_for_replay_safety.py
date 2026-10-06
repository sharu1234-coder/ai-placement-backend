"""add operation_claims for replay safety

Phase 24-B. One new table, nothing altered, nothing moved.

Why it is needed. A client cannot distinguish "my request never arrived" from "it arrived,
committed, and the reply was lost" -- a successful TCP write says nothing about the far end. So
OkHttp's connection retry can deliver the same mutating POST twice, and twenty-two of this app's
POST handlers insert unconditionally. Phase 24 tried to solve that on the client by switching the
retry off for those routes, and a live device run showed the cost: the first tap after the phone
had been idle failed with "Couldn't reach the server" while the server log showed the request had
never arrived -- the retry had been recovering from a dead pooled connection, correctly.

The retry therefore stays, and the duplicate is collapsed server-side instead. `operation_claims`
is where that happens.

Shape, and why each part is here:

- `UNIQUE (profile_id, idempotency_key)` is the entire enforcement mechanism. Two concurrent
  duplicates cannot both insert: one wins, the other takes an IntegrityError that the service
  reads as "my own duplicate got here first". There is no check-then-act window, and the
  guarantee holds across processes because the storage engine enforces it rather than Python.

- `request_fingerprint` stops the key being a re-entry token. Same key with different contents
  is a conflict, not a silent substitution of the first operation's result.

- `result_kind` / `result_id` record what was produced, as a kind and a row id. Deliberately not
  a serialised response: a replay re-reads the live row, so the answer cannot go stale, and no
  request or response body is ever persisted here. A resume upload stores a hash of the file.

- `ix_operation_claims_created_at` serves retention. Settled rows are swept per profile once they
  are well past the replay window, which needs no scheduler -- the same approach
  password_reset_service already uses for its tokens.

No batch_alter_table: this only creates a table, which SQLite handles natively, so the same DDL
runs on SQLite (local development, the whole test suite) and PostgreSQL. The downgrade drops the
table and its indexes, and loses nothing but deduplication history -- no student-visible record
lives here, which is exactly why it is safe to drop.

Revision ID: 7f3c21d9ab48
Revises: 5b8e2f7c1a94
Create Date: 2026-10-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7f3c21d9ab48"
down_revision: Union[str, None] = "5b8e2f7c1a94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operation_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=60), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_kind", sa.String(length=60), nullable=True),
        sa.Column("result_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["student_profiles.id"],
            name="fk_operation_claims_profile_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "idempotency_key", name="uq_operation_claims_idempotency"
        ),
    )
    op.create_index(
        "ix_operation_claims_profile_id", "operation_claims", ["profile_id"], unique=False
    )
    op.create_index("ix_operation_claims_status", "operation_claims", ["status"], unique=False)
    op.create_index(
        "ix_operation_claims_profile_status",
        "operation_claims",
        ["profile_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_operation_claims_created_at", "operation_claims", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_operation_claims_created_at", table_name="operation_claims")
    op.drop_index("ix_operation_claims_profile_status", table_name="operation_claims")
    op.drop_index("ix_operation_claims_status", table_name="operation_claims")
    op.drop_index("ix_operation_claims_profile_id", table_name="operation_claims")
    op.drop_table("operation_claims")
