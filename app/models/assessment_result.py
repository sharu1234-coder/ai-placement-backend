import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class AssessmentResult(Base):
    __tablename__ = "assessment_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessments.id"), nullable=False, unique=True, index=True
    )
    skill_scores: Mapped[dict] = mapped_column(JSON, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    priority_weaknesses: Mapped[list] = mapped_column(JSON, nullable=False)
    # Stage 12: needed for the Progress dashboard's trend view -- nothing in the schema
    # recorded *when* a result was produced before this (see PROJECT_HANDOFF.md section
    # 8.5's "aggregate readiness/trend").
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
