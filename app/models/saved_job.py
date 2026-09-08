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
# Where a tracked job came from. Provenance is recorded at save time and never recomputed:
# working it out later from what a job looks like now would give a different answer once the
# student's profile or the saved search changed, and the tracker would quietly rewrite its own
# history. "manual" is the honest default for a row saved before this column existed.
SAVED_JOB_SOURCES = ("recommended", "saved_search", "job_alert", "manual")

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

    # How this job reached the student. See SAVED_JOB_SOURCES.
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    # Which saved search, when source == "saved_search". Lets the tracker name it ("Saved
    # Search: Dubai Data Analyst") rather than showing an unhelpfully generic label. Nullable
    # for every other source, and left dangling rather than cascading if the search is later
    # deleted -- the fact that this job came from a search the student has since removed is
    # still true, and rewriting it to "manual" would falsify their own history.
    source_saved_search_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("job_saved_searches.id"), nullable=True, index=True
    )
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
