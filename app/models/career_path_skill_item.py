from typing import Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CareerPathSkillItem(Base):
    """One skill within one phase of a CareerPath. skill_key is a stable catalog slug (NOT
    a DB-generated id) -- it's what lets a refresh/regenerate carry forward a completed
    item's status even though the row itself is brand new (mirrors
    PlacementPathTopic.topic_key's exact purpose, but implemented independently here --
    see career_path/service.py). practice_blueprint_id is lazily created only when the
    student first taps "Practice" for this skill."""

    __tablename__ = "career_path_skill_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    career_path_id: Mapped[int] = mapped_column(ForeignKey("career_paths.id"), nullable=False, index=True)
    phase_key: Mapped[str] = mapped_column(String(30), nullable=False)
    skill_key: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="beginner")
    why_it_matters: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(nullable=False)
    practice_blueprint_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assessment_blueprints.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
    completed_via: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
