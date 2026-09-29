import datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ReadinessSnapshot(Base):
    """One row per (profile_id, role_family_key, snapshot_date) -- at most one per profile per
    role per day, upserted opportunistically whenever Career Twin is read (see
    career_twin_service.get_current). Exists purely so an evidence-coverage trend delta (e.g.
    "+6% this week") is a real diff against a value genuinely computed on a real prior day,
    never a guess. Optional/additive: Career Twin works with zero snapshots --
    evidence_coverage_trend_percent is simply None until two days' worth exist for the role.

    The table name stays `readiness_snapshots`: it identifies a table, not a measurement, and
    renaming it would turn a one-column migration into a table rebuild for no semantic gain."""

    __tablename__ = "readiness_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    role_family_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    snapshot_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    # EVIDENCE COVERAGE, not readiness. Renamed from readiness_percent in Phase 2G: this is
    # the weighted mean of evidence confidence across the role's skills, which is what Career
    # Twin has always computed. Readiness in this product is JobReadinessRead.overall_percent,
    # a five-signal average, and one word must not name two formulas.
    #
    # Still NOT NULL, because a row here asserts that a measurement was taken. Career Twin
    # writes no row at all when the value is unmeasurable (see career_twin_service.get_current).
    evidence_coverage_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    # {skill: confidence_percent} at snapshot time -- lets a future per-skill trend view exist
    # without needing a second table, though only the overall coverage figure is used today.
    per_skill_confidence_percent: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("profile_id", "role_family_key", "snapshot_date", name="uq_readiness_snapshot_per_day"),
    )
