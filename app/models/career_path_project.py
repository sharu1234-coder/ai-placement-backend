from typing import Optional

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CareerPathProject(Base):
    """A role-appropriate portfolio project recommendation within a CareerPath's "projects"
    phase. Deliberately a dedicated table rather than JSON-on-CareerPath or a
    CareerPathSkillItem -- projects carry many structured fields (milestones, GitHub
    expectations, resume-bullet suggestions, interview questions) that don't fit a
    single-skill row. project_key is a stable catalog slug for carry-forward, mirroring
    skill_key. Completion is manual-only (no assessment tie-in, unlike skill items)."""

    __tablename__ = "career_path_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    career_path_id: Mapped[int] = mapped_column(ForeignKey("career_paths.id"), nullable=False, index=True)
    project_key: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    difficulty: Mapped[str] = mapped_column(String(20), nullable=False, default="beginner")

    skills_demonstrated: Mapped[list] = mapped_column(JSON, nullable=False)
    technologies: Mapped[list] = mapped_column(JSON, nullable=False)

    problem_statement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expected_outcome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    what_should_be_included: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    why_recruiters_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resume_bullet_suggestion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    interview_questions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    order_index: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
