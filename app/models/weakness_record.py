import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class WeaknessRecord(Base):
    """Cross-interview memory for one (profile_id, skill_key) -- skill_key is an interview
    evaluation dimension tag (e.g. "interview:technical_correctness"), the same tags
    weakness_service.compute_weaknesses already produces live from raw InterviewEvaluation
    rows. That live view has no memory of its own: it can't tell "flagged weak for the first
    time" apart from "flagged weak in five straight interviews," and a single lucky good
    answer would look identical to genuine, sustained improvement. This table is exactly the
    state needed to tell those apart -- see weakness_record_service.record_or_update for the
    status machine (NEW -> REPEATED -> IMPROVING -> NEARLY_CLOSED -> CLOSED) built on top of
    it. A row is created only the first time a dimension is ever flagged weak; dimensions that
    have never been weak are never tracked here at all.
    """

    __tablename__ = "weakness_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    skill_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="NEW")
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Resets to 0 on every weak signal -- only a run of good signals with no weak signal
    # in between counts toward advancing status. See the module docstring.
    consecutive_good_signals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_score: Mapped[float] = mapped_column(Float, nullable=False)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    __table_args__ = (UniqueConstraint("profile_id", "skill_key", name="uq_weakness_record_per_skill"),)
