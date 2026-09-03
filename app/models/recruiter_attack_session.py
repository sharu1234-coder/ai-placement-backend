import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class RecruiterAttackSession(Base):
    """A single 'recruiter attack' round on one real, verifiable claim -- either a completed
    ProjectBuild or a substring the student picked out of their own uploaded resume (never a
    freeform, unverified claim -- see recruiter_attack_service.start_session's substring
    check). Grading is session-level only (technical_defense/project_ownership/specificity/
    communication + overall_score + feedback_text), computed once lazily via get_results and
    cached directly on this row -- same lazy-eval-then-cache shape as InterviewEvaluation,
    deliberately not a separate table since there is exactly one evaluation per session.
    """

    __tablename__ = "recruiter_attack_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)  # project_build | resume_claim
    source_label: Mapped[str] = mapped_column(String(300), nullable=False)
    source_project_build_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("project_builds.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")

    technical_defense: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    project_ownership: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    specificity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    communication: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    overall_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    feedback_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
