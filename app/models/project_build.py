import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ProjectBuild(Base):
    """A student's own guided, staged build of one real project (see ProjectBuildStage for
    the 10 fixed stages) -- distinct from CareerPathProject, which is only a *recommendation*
    the student hasn't necessarily started, and from the flat Project model, which is a
    plain, unstructured profile entry with no build process at all. role_family_key/
    target_role_name are a snapshot (via role_context.resolve_role_context) taken at creation
    time, not a live FK to CareerPath -- a build must stay meaningful even if the student
    later changes their target role. Proof fields are cached here (not a separate table) once
    generated on full completion -- see project_proof_service."""

    __tablename__ = "project_builds"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    role_family_key: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    target_role_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    stack_summary: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")

    proof_resume_bullet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    proof_readme_markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    proof_explanation_60s: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    proof_explanation_5min: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    proof_defense_questions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # GitHub Project Verification (Phase 8) -- unauthenticated, public-repos-only. Cached here
    # (not re-fetched from GitHub on every read) so github_verification_service.check_repository
    # is called only when the student explicitly (re-)runs a check. See that module's docstring
    # for why this is "evidence detected", never "verified" -- a public repo existing is a real
    # but limited signal, not proof of skill quality.
    github_repo_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    github_evidence: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
