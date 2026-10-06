import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
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
    # Added after this table shipped, so all three are nullable: rows written before the
    # columns existed keep reading back fine, and a NULL here means "this plan predates the
    # section", which is a different thing from an empty section and is reported as such.
    gap_guidance: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    resume_recommendations: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    project_connections: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    __table_args__ = (
        # One authoritative plan per student per job description -- the business invariant the
        # endpoint had always behaved as if it had, and nothing enforced. Both read paths take
        # the newest row for a (profile, jd) and discard any other, so a second row was never
        # visible to the student; it was a duplicate the database kept and an AI call they paid
        # for. Two requests that both find no plan now collide here instead of both inserting.
        #
        # Enforced in addition to, not instead of, the generation claim in idempotency.py. The
        # claim is what stops the second expensive generation from starting; this is what makes
        # the invariant permanent, because settled claims are swept after RETENTION while a plan
        # is kept forever.
        UniqueConstraint("profile_id", "jd_id", name="uq_job_preparation_plan_per_jd"),
    )
