"""Job alerts: a standing instruction to look for new jobs and tell the student about them.

Two tables, and the split matters. `job_alerts` is the student's *instruction* -- what to look
for, how often, and when not to disturb them. `job_alert_deliveries` is the record of what was
actually sent, one row per (alert, listing), and it exists for one reason above all others:
without it, the same advert is announced every single time the alert runs. A student who is
notified about the same job every morning for a week stops reading the notifications, and then
the feature has made the product worse than not having it.

Consent is explicit and per-alert. There is no global "alerts on" switch that silently opts
every saved search in -- a student who wanted one alert about data-engineering roles in Dubai
did not thereby ask to be told about everything else they ever searched for.
"""

import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

# How often an alert may run. Deliberately coarse: "hourly" would be a promise this product
# cannot keep (job providers publish on their own schedule and most rate-limit hard), and
# offering it would be advertising a frequency we would then silently fail to honour.
ALERT_FREQUENCIES = ("daily", "weekly")

# Terminal-ish states an alert run can end in, recorded so the student can be told the truth
# about why an alert has gone quiet rather than being left to assume there are no jobs.
ALERT_RUN_STATUSES = ("success", "no_new_jobs", "provider_error", "not_configured", "skipped_quiet_hours")


class JobAlert(Base):
    """One standing search the student wants to be told about.

    Bound to a saved search when it was created from one, so editing the search's filters
    changes what the alert looks for -- two copies of the same filter set would drift, and the
    student would have no way to tell which one the alert was actually using. `filters` is only
    populated for a standalone alert that has no saved search behind it.
    """

    __tablename__ = "job_alerts"
    __table_args__ = (
        # One alert per saved search. A second alert on the same search would just double every
        # notification, which is the exact failure the delivery table exists to prevent.
        UniqueConstraint("profile_id", "saved_search_id", name="uq_job_alert_per_saved_search"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )
    # Nullable: an alert can stand on its own filters without a saved search behind it.
    # ondelete is not declared here because deletion is handled explicitly in
    # account_service.py, in an order chosen to satisfy real foreign keys.
    saved_search_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("job_saved_searches.id"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Only used when saved_search_id is NULL. See the class docstring for why these are never
    # both populated.
    filters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="daily")
    # 0=Monday..6=Sunday, only meaningful for a weekly alert. Kept as a plain integer rather
    # than a weekday enum so it survives a locale change: "Monday" is Monday everywhere, but
    # the *first* day of the week is not.
    day_of_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Local hour the student wants to hear from us, 0-23.
    hour_local: Mapped[int] = mapped_column(Integer, nullable=False, default=9)

    # Quiet hours, as local hours. A window that wraps midnight (22 -> 7) is normal and is
    # handled by the service rather than being forbidden here.
    quiet_hours_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quiet_hours_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # The consent itself. Defaults to off: creating an alert and enabling it are two separate
    # acts, and defaulting to on would opt a student in by the act of setting one up.
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Run bookkeeping. Three distinct timestamps rather than one, because "we ran and found
    # nothing", "we ran and the provider failed" and "we have never run" are three different
    # things and a student deserves to be told which one they are looking at.
    last_run_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_error_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_run_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # New jobs announced on the most recent run. NULL means never run -- never zero, which
    # would read as "we looked and there is nothing for you".
    last_new_job_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class JobAlertDelivery(Base):
    """One (alert, listing) pair that has already been announced.

    The unique constraint is the whole mechanism. Duplicate suppression implemented in service
    code alone would fail the moment two runs overlap -- exactly what happens when a run is
    slow and the next one starts -- so the database enforces it and the service treats an
    IntegrityError as "already told them", not as an error.

    Also the alert history the student can read: what was sent, and when.
    """

    __tablename__ = "job_alert_deliveries"
    __table_args__ = (
        UniqueConstraint("alert_id", "listing_id", name="uq_alert_delivery_per_listing"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("job_alerts.id"), nullable=False, index=True)
    # Denormalized from the alert so history can be listed and access-checked for one student
    # without joining, and so deletion by profile stays a single statement.
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )
    listing_id: Mapped[int] = mapped_column(ForeignKey("job_listings.id"), nullable=False, index=True)

    notified_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
