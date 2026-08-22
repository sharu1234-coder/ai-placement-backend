from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class RoleSkillWeight(Base):
    """Deviates from PROJECT_HANDOFF.md's `skill_id FK` the same way SkillGap does (see
    that model's docstring) -- but here the doc's own Pydantic schema (section 8.7,
    RoleSkillWeight) already uses `skill: str`, not an id, so this follows the doc's more
    authoritative Python schema over its looser prose entity list."""

    __tablename__ = "role_skill_weights"

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False, index=True)
    skill: Mapped[str] = mapped_column(String(100), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    # aptitude/technical/sql/coding/case_study/statistics/tool -- matches RoleSkillWeight schema
    assessment_type: Mapped[str] = mapped_column(String(20), nullable=False)
