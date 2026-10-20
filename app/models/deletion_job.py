import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_NEEDS_ATTENTION = "needs_attention"

UNRESOLVED_STATUSES = (STATUS_PENDING, STATUS_NEEDS_ATTENTION)


class DeletionJob(Base):
    """One stored object to erase, with the key copied out before its row was deleted.

    This row is the only remaining pointer to the object once the resume row is gone, which
    sets the ordering rule for Phase 1: the job is inserted, and the row that held the key is
    deleted, in the same transaction. If that transaction rolls back, both survive. There is
    no interleaving in which the last pointer is destroyed while the object is still there.

    `storage_key` is personal data by association -- it is a user-scoped path -- so it is
    never logged. Operators get `deletion_request.request_uuid` and this row's id.
    """

    __tablename__ = "deletion_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    deletion_request_id: Mapped[int] = mapped_column(
        ForeignKey("deletion_requests.id"), nullable=False, index=True
    )
    # Recorded per job rather than read from configuration at execution time: the provider that
    # held the object is a fact about the object, and configuration can change between the
    # request and the erasure.
    storage_provider: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # When the job becomes claimable again. Set on creation so the first claim needs no special
    # case, and pushed out by backoff after a transient failure.
    next_attempt_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The lease. A claim sets it; a crash lets it expire; expiry is what makes the job
    # reclaimable without anyone having to notice the crash.
    locked_until: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The exception *class*, never its message: a message can carry a key or a bucket name.
    # Suffixed with the backend's own error code when it supplies one ("StorageError:AccessDenied",
    # "StorageError:EACCES"), which is what lets an operator tell a refusal that needs a human
    # from one that will clear on its own. "ProviderMismatch" means the job was enqueued for a
    # different backend than the worker held, and nothing was deleted.
    last_error_class: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
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
        # Makes Phase 1 idempotent at the database level: a retried request cannot enqueue the
        # same object twice, whatever the service layer does.
        UniqueConstraint("deletion_request_id", "storage_key", name="uq_deletion_jobs_request_key"),
        # The claim query's access path: status first, then due time.
        Index("ix_deletion_jobs_status_next_attempt_at", "status", "next_attempt_at"),
    )
