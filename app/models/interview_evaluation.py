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
    technical_correctness: Mapped[float] = mapped_column(Float, nullable=False)
    relevance: Mapped[float] = mapped_column(Float, nullable=False)
    completeness: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[float] = mapped_column(Float, nullable=False)
    structure: Mapped[float] = mapped_column(Float, nullable=False)
    clarity: Mapped[float] = mapped_column(Float, nullable=False)
    grammar: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # text mode
    speaking_pace: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # voice mode (Stage 10)
    filler_word_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # voice mode (Stage 10)
    role_jd_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    feedback_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Stage 12: see the matching note on AssessmentResult.created_at.
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
