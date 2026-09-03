import datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ReadinessSnapshot(Base):
    """One row per (profile_id, role_family_key, snapshot_date) -- at most one per profile per
    role per day, upserted opportunistically whenever Career Twin is read (see
    career_twin_service.get_current). Exists purely so a readiness trend delta (e.g. "+6% this
    week") is a real diff against a value genuinely computed on a real prior day, never a
    guess. Optional/additive: Career Twin works with zero snapshots -- readiness_trend_percent
    is simply None until at least two days' worth exist for the same role."""

    __tablename__ = "readiness_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    role_family_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    snapshot_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    readiness_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    # {skill: confidence_percent} at snapshot time -- lets a future per-skill trend view exist
    # without needing a second table, though only the overall readiness_percent is used today.
    per_skill_confidence_percent: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("profile_id", "role_family_key", "snapshot_date", name="uq_readiness_snapshot_per_day"),
    )
