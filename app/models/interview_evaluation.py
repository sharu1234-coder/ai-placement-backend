import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class InterviewEvaluation(Base):
    __tablename__ = "interview_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[int] = mapped_column(
        ForeignKey("interviews.id"), nullable=False, unique=True, index=True
    )
    # All eight graded dimensions are nullable, and NULL means exactly one thing: this
    # interview was finished and grading was attempted, but the AI evaluation did not produce a
    # usable result. It never means zero. Nothing may substitute a number for it -- a fabricated
    # score here would not merely be shown to the student, it would flow through Interview
    # Memory and the Evidence Engine into their Career Twin readiness.
    #
    # `overall_score IS NULL` is the single source of truth for "ungraded"; the API's is_graded
    # flag is derived from it rather than stored, so the two cannot drift apart. Same shape, and
    # the same reasoning, as RecruiterAttackSession's nullable scores.
    technical_correctness: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    relevance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    completeness: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reasoning: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    structure: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    clarity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Nullable for a different reason -- these three are mode-specific, not grade-dependent.
    grammar: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # text mode
    speaking_pace: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # voice mode (Stage 10)
    filler_word_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # voice mode (Stage 10)
    role_jd_coverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    overall_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Stays NOT NULL: an ungraded evaluation still owes the student an explanation of why there
    # is no score, so there is always something to say here.
    feedback_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Stage 12: see the matching note on AssessmentResult.created_at.
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
