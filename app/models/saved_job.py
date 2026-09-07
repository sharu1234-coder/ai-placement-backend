import datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

# The application pipeline, in order. Kept here (not in the service) because both the model's
# own validation and the API schema need the same list, and a second copy would be free to
# drift. "dismissed" is deliberately part of the same state machine rather than a separate
# boolean: a student who dismisses a job and later saves it is one row changing status, which
# keeps the unique constraint below meaningful.
SAVED_JOB_STATUSES = (
    "saved",
    "applied",
    "assessment",
    "interview",
    "offer",
    "rejected",
    "closed",
    "dismissed",
)


class SavedJob(Base):
    """A student's own relationship with one listing: saved, dismissed, or somewhere in the
    application pipeline.

    One row per (profile, listing) -- saving a job twice updates the existing row rather than
    creating a duplicate pipeline entry, which is what makes the concurrent-save test in
    tests/api/test_job_hunt.py meaningful rather than a race that silently produces two rows.
    """

    __tablename__ = "saved_jobs"
    __table_args__ = (UniqueConstraint("profile_id", "listing_id", name="uq_saved_job_per_profile"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )
    listing_id: Mapped[int] = mapped_column(ForeignKey("job_listings.id"), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="saved")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    follow_up_date: Mapped[Optional[datetime.date]] = mapped_column(Date, nullable=True)

    # Set the first time status moves to "applied" and never rewritten afterwards, so the
    # tracker can show how long an application has been outstanding. Distinct from updated_at,
    # which moves on every note edit.
    applied_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
