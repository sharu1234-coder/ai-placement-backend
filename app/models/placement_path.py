import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PlacementPath(Base):
    """One row per generated personalized roadmap. A profile can accumulate many rows over
    time (status="superseded" on regeneration) -- the active one is always the single
    status="active" row, mirroring Roadmap's own active/superseded convention. Old rows are
    never deleted, so completed-topic history from a superseded path is always recoverable."""

    __tablename__ = "placement_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    source_resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resumes.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    __table_args__ = (
        # At most one active path per student -- the invariant the docstring above has always
        # described ("the active one is always the single status=active row") and that nothing
        # enforced. Two concurrent generations both read "no other active path", both superseded
        # the same old row, and both inserted an active one; `get_current_placement_path` then
        # picked between them with an unordered .first(), so which roadmap the student saw was
        # decided by row order.
        #
        # A *partial* unique index, not UNIQUE(profile_id): superseded rows are history and a
        # profile accumulates them for good, so the column itself cannot be unique. Both engines
        # this app runs on support the predicate, which is why it is written for each rather than
        # left to a portable approximation that would forbid the history.
        #
        # Enforced in addition to, not instead of, the generation claim in idempotency.py. The
        # claim is what stops the second 163-second generation from starting; this is what makes
        # "one active path" true of the table itself, including down any path that never consults
        # the guard.
        Index(
            "uq_placement_path_one_active_per_profile",
            "profile_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )
