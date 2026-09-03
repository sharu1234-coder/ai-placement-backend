import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ArenaAttempt(Base):
    """One timed attempt at a Placement Arena challenge. assessment_id points at a real
    Assessment created via the same code path Prepare Me uses (a single-section
    AssessmentBlueprint for the challenge's skill) -- Arena adds timing + history + a
    personal-best comparison on top of the existing, already-tested Assessment engine, it
    never reimplements question generation or grading (see arena_service's module docstring).

    score/is_personal_best/previous_best_score are persisted here (not recomputed at read
    time) so that a duplicate or racing completion call always returns the exact same result
    the first successful completion produced -- see arena_service.complete_attempt's atomic
    conditional UPDATE.
    """

    __tablename__ = "arena_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    challenge_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")

    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    time_taken_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Determined server-side from created_at vs. the challenge's own time_limit_seconds --
    # never a client-supplied flag (see arena_service.complete_attempt).
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_personal_best: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    previous_best_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
