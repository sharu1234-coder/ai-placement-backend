import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PlacementPath(Base):
    """One row per generated personalized roadmap. A profile can accumulate many rows over
    time (status="superseded" on regeneration) -- the active one is always the single
    status="active" row, mirroring Roadmap's own active/superseded convention. Old rows are
    never deleted, so completed-topic history from a superseded path is always recoverable."""

    __tablename__ = "placement_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    source_resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resumes.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
