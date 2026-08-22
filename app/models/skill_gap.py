from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class SkillGap(Base):
    """Deviates from PROJECT_HANDOFF.md's `skill_id FK`: there is no global skill catalog
    table (see Stage 4 notes -- Skill is per-profile only), so gaps are keyed by name,
    matching how JobMatch.matching_skills/missing_skills are also name-based, not FK-based."""

    __tablename__ = "skill_gaps"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_match_id: Mapped[int] = mapped_column(ForeignKey("job_matches.id"), nullable=False, index=True)
    skill_name: Mapped[str] = mapped_column(String(100), nullable=False)
    gap_severity: Mapped[str] = mapped_column(String(20), nullable=False)  # high/medium
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="jd_match")
