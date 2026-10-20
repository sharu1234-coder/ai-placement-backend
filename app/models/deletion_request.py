import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

# The request is open until every object it owns is gone.
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_NEEDS_ATTENTION = "needs_attention"

# What a downgrade, and the worker's "is there anything left" query, treat as unfinished.
# `needs_attention` is here deliberately: it means "visible and still being retried", never
# "given up on". See docs/proposal-account-deletion-outbox.md section 11.2.
UNRESOLVED_STATUSES = (STATUS_PENDING, STATUS_NEEDS_ATTENTION)


class DeletionRequest(Base):
    """One account erasure, durable from the moment access is revoked.

    `original_user_id` carries **no foreign key to `users`**, and that absence is the point of
    the table. An enforced reference would make the user row undeletable while jobs remained,
    which inverts the invariant: the request exists precisely so the user row can go while the
    obligation to erase their objects survives. Anyone "fixing" this by adding the constraint
    reintroduces the bug -- the migration says so too.

    No erasure receipt is kept by default. The row is retained while it is unresolved because
    the obligation needs somewhere to live, and it holds no profile content: an opaque uuid, an
    integer that no longer resolves to anybody, and timestamps.
    """

    __tablename__ = "deletion_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable handle for operators and logs. Safe to print: it identifies the request, not the
    # person, and the user row it came from is gone by the time anyone needs it.
    request_uuid: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    original_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PENDING)
    # Set inside the Phase 1 transaction, so the alerting clock starts the moment access was
    # revoked rather than when a worker first looked.
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Drives both the operator sweep and the alert thresholds, which are measured from
        # created_at.
        Index("ix_deletion_requests_status_created_at", "status", "created_at"),
    )
