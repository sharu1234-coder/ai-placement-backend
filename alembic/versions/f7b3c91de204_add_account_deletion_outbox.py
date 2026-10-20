"""add account deletion outbox

Two additive tables. Nothing existing is altered: no column is added to `users`, no type is
changed, and there is no data backfill. The single head d4a1c08f52e7 is preserved.

Revision ID: f7b3c91de204
Revises: d4a1c08f52e7
Create Date: 2026-10-20

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from app.database.downgrade_guard import refuse_if_outstanding

revision: str = "f7b3c91de204"
down_revision: Union[str, None] = "d4a1c08f52e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_uuid", sa.String(length=36), nullable=False),
        # ---------------------------------------------------------------------------------
        # DELIBERATELY NO FOREIGN KEY to users.id. Do not add one.
        #
        # This row's whole purpose is to outlive the user row. An enforced reference would
        # make the user undeletable while erasure jobs remained, which inverts the invariant
        # the table exists to protect: the account must become inaccessible and then gone,
        # while the obligation to erase their stored objects survives independently.
        #
        # The consequence is that the value stops resolving to anybody once the user row is
        # deleted. That is intended -- it is a historical identifier, not a reference -- and
        # it is why the worker tolerates `db.get(User, ...)` returning None.
        # ---------------------------------------------------------------------------------
        sa.Column("original_user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_uuid", name="uq_deletion_requests_request_uuid"),
    )
    op.create_index(
        "ix_deletion_requests_status_created_at",
        "deletion_requests",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "deletion_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deletion_request_id", sa.Integer(), nullable=False),
        sa.Column("storage_provider", sa.String(length=20), nullable=False),
        # The copied pointer. Once the resume row is gone this is the only record that the
        # object exists, which is why Phase 1 inserts it in the same transaction as that
        # delete.
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # This FK *is* wanted: a job without its request is meaningless, and the request is
        # only deleted once every job under it is resolved.
        sa.ForeignKeyConstraint(["deletion_request_id"], ["deletion_requests.id"]),
        # Makes a retried Phase 1 idempotent in the database rather than in the service: the
        # same object cannot be enqueued twice under one request.
        sa.UniqueConstraint("deletion_request_id", "storage_key", name="uq_deletion_jobs_request_key"),
    )
    op.create_index(
        "ix_deletion_jobs_deletion_request_id", "deletion_jobs", ["deletion_request_id"], unique=False
    )
    op.create_index(
        "ix_deletion_jobs_status_next_attempt_at",
        "deletion_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Refuse while any erasure is unfinished.

    Dropping these tables destroys the only remaining record of objects a person asked to have
    erased. So this checks first, and raises if anything is outstanding. There is deliberately
    **no override flag**: a switch that let the drop proceed anyway would be the mechanism by
    which an obligation silently disappears, and someone under deployment pressure would use
    it. The documented procedure is to drain the queue -- run the worker until no request is
    unresolved -- and then downgrade.

    `needs_attention` counts as unresolved. It means "still being retried and visible", not
    "abandoned".
    """
    refuse_if_outstanding(
        op.get_bind(),
        table="deletion_requests",
        unresolved=("pending", "needs_attention"),
        obligation="account erasure request",
    )

    op.drop_index("ix_deletion_jobs_status_next_attempt_at", table_name="deletion_jobs")
    op.drop_index("ix_deletion_jobs_deletion_request_id", table_name="deletion_jobs")
    op.drop_table("deletion_jobs")
    op.drop_index("ix_deletion_requests_status_created_at", table_name="deletion_requests")
    op.drop_table("deletion_requests")
