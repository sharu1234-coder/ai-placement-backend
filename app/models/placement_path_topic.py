from typing import Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PlacementPathTopic(Base):
    """One row per topic in a PlacementPath's tree. Deliberately does NOT store priority,
    prerequisite_topic_id, or a "locked" flag -- all three are computed live at read time
    (see placement_path_rules.py): priority and lock-state depend on the student's current
    progress/assessment performance and would go stale exactly the way persisted state
    shouldn't; the prerequisite is always "same track_key, order_in_track - 1" within the
    same path, so no self-referential FK is needed. topic_key is a stable catalog slug (e.g.
    "sql_joins"), NOT a DB-generated id -- it's what lets regeneration carry forward a
    completed topic's status even though the row itself is brand new (see
    placement_path_service.generate_placement_path)."""

    __tablename__ = "placement_path_topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    path_id: Mapped[int] = mapped_column(ForeignKey("placement_paths.id"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    track_key: Mapped[str] = mapped_column(String(50), nullable=False)
    topic_key: Mapped[str] = mapped_column(String(50), nullable=False)
    order_in_track: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    skill_tag: Mapped[str] = mapped_column(String(100), nullable=False)
    why_it_matters: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    practice_blueprint_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assessment_blueprints.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
    completed_via: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
