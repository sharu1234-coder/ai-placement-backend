import datetime
from typing import Optional

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Mission(Base):
    """One row per (profile_id, mission_date) -- Today's Mission is always exactly one real,
    already-computed Next Best Action recommendation, snapshotted at creation time so it stays
    stable for the rest of the day even if the student's underlying evidence changes mid-day
    (e.g. completing an unrelated assessment shouldn't swap today's mission out from under
    them). mission_service.get_or_create_today is the only writer of a new row; the unique
    constraint is what actually guarantees "at most one mission per profile per day" under
    concurrent creation, same pattern as ReadinessSnapshot's per-day guarantee."""

    __tablename__ = "missions"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    mission_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)

    # Snapshotted from NextBestActionRead at creation time -- see the class docstring.
    tier: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    action_type: Mapped[str] = mapped_column(String(20), nullable=False)
    estimated_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    skill: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
    started_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    # The real Assessment the student actually completed to close out this mission, if any --
    # never a fabricated score/delta. See mission_service.complete.
    assessment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("assessments.id"), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    __table_args__ = (UniqueConstraint("profile_id", "mission_date", name="uq_mission_per_day"),)
