from typing import Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CareerPathPhase(Base):
    """One of a CareerPath's fixed 5 phases (foundations, core_skills, projects,
    interview_prep, job_ready). Deliberately does NOT store a status column -- like
    PlacementPathTopic's priority/lock, phase status is computed live from its children
    (see career_path/rules.compute_phase_status) so it can never go stale relative to the
    student's actual skill-item/project completion."""

    __tablename__ = "career_path_phases"

    id: Mapped[int] = mapped_column(primary_key=True)
    career_path_id: Mapped[int] = mapped_column(ForeignKey("career_paths.id"), nullable=False, index=True)
    phase_key: Mapped[str] = mapped_column(String(30), nullable=False)
    order_index: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    objective: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
