import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PlacementAlarmChallenge(Base):
    """One row per on-device alarm firing that has generated its 6 questions.
    client_alarm_id is an opaque id the Android app generates when an alarm fires (it owns
    scheduling entirely -- see the Room-only alarm-config design) -- looking a challenge up
    by (profile_id, client_alarm_id, status="in_progress") is what makes start_challenge
    idempotent, so reopening the same still-active challenge (after Back/app-close/process
    death) returns the exact same 6 questions rather than generating a new random set."""

    __tablename__ = "placement_alarm_challenges"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    client_alarm_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
