import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class JobPreparationPlan(Base):
    """A saved, point-in-time snapshot of a JD analysis -- not live-recomputed. Deliberately
    a single JSON-blob-per-row (like ResumeAnalysis), not normalized per-item rows like
    PlacementPathTopic/RoadmapTask, because nothing here needs independent per-item status
    mutation from the UI; it's a frozen "save/reopen/delete" analysis result."""

    __tablename__ = "job_preparation_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    jd_id: Mapped[int] = mapped_column(ForeignKey("job_descriptions.id"), nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    job_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    job_summary: Mapped[dict] = mapped_column(JSON, nullable=False)
    required_skills: Mapped[list] = mapped_column(JSON, nullable=False)
    preparation_gaps: Mapped[list] = mapped_column(JSON, nullable=False)
    preparation_plan: Mapped[list] = mapped_column(JSON, nullable=False)
    interview_prep: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
