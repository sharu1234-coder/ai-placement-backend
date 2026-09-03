import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class EvidenceStateSnapshot(Base):
    """The last evidence_engine state we told the notification system about, per
    (profile_id, skill_key) -- NOT a second source of truth for evidence itself (that stays
    entirely computed live by evidence_engine from real assessment/practice/project/interview/
    resume rows, per its own docstring). This table exists purely so career_twin_service can
    detect "did this skill's state just cross into strong/verified" on a later read, the same
    narrowly-scoped exception to the "no persisted evidence" rule that ReadinessSnapshot
    already is for the readiness trend."""

    __tablename__ = "evidence_state_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    skill_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    __table_args__ = (UniqueConstraint("profile_id", "skill_key", name="uq_evidence_state_snapshot"),)
