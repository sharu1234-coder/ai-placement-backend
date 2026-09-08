import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class JobSavedSearch(Base):
    """A named, re-runnable set of search filters.

    `filters` holds the same validated JobSearchFilters shape the search endpoint accepts, so
    re-running a saved search is literally replaying a request the student already made rather
    than a second, parallel filter format that could drift from it.

    The run-metadata columns are written by the re-run endpoint
    (POST /job-hunt/saved-searches/{id}/run) and by the alert evaluator, and only ever after a
    genuinely completed provider call -- never after a failed or rate-limited one, which
    records last_error_at instead. NULL still means "never run", and the API contract keeps
    treating it as such rather than as zero: a zero would read to the student as "there are no
    jobs for this search" when the truth is "we have not asked yet".
    """

    __tablename__ = "job_saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Three timestamps, not one. "we have never run this", "we ran it and the provider
    # failed" and "we ran it successfully" are three different things, and collapsing them
    # into a single last_run_at leaves the UI unable to tell a student why their search has
    # gone quiet. last_result_count is only ever written alongside last_success_at.
    last_run_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_error_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_result_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Whether the student wants alerts for this search. Consent is per-search and defaults to
    # off (spec section 16: "honor consent") -- never inherited from a global notification
    # toggle, which would silently opt every saved search in.
    notify_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
