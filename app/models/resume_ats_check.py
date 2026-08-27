import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ResumeAtsCheck(Base):
    """A saved, point-in-time ATS readiness check -- JSON-blob-per-row like
    JobPreparationPlan, never mutated after creation. Deliberately NOT merged into
    ResumeAnalysis: ResumeAnalysis.ats_score is AI-derived (from resume_parser_v1.txt) and
    1:1 with a Resume, so it can neither hold a deterministic score nor a history of
    multiple re-checks of the same resume. This table is 1:many with Resume by design."""

    __tablename__ = "resume_ats_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id"), nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    target_role: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    overall_score: Mapped[int] = mapped_column(Integer, nullable=False)
    score_category: Mapped[str] = mapped_column(String(30), nullable=False)

    # Flat, individually-named per the product spec -- also gives cheap direct SQL/debug
    # access and simple boundary-test assertions without JSON decoding. Written from the
    # same ScoringResult computation as category_breakdown below, so the two can never drift.
    parsing_score: Mapped[int] = mapped_column(Integer, nullable=False)
    structure_score: Mapped[int] = mapped_column(Integer, nullable=False)
    content_score: Mapped[int] = mapped_column(Integer, nullable=False)
    skills_score: Mapped[int] = mapped_column(Integer, nullable=False)
    experience_score: Mapped[int] = mapped_column(Integer, nullable=False)
    projects_score: Mapped[int] = mapped_column(Integer, nullable=False)
    contact_score: Mapped[int] = mapped_column(Integer, nullable=False)
    readability_score: Mapped[int] = mapped_column(Integer, nullable=False)

    category_breakdown: Mapped[list] = mapped_column(JSON, nullable=False)
    section_analysis: Mapped[list] = mapped_column(JSON, nullable=False)
    issues: Mapped[list] = mapped_column(JSON, nullable=False)
    recommendations: Mapped[list] = mapped_column(JSON, nullable=False)

    potential_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    potential_score_explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # NOT NULL -- always a real sentence, either AI-written or the deterministic template
    # fallback (see ats_check_service.generate_ats_check). Never left blank.
    ai_summary: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
