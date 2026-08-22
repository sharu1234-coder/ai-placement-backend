from sqlalchemy import JSON, Float, ForeignKey
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
