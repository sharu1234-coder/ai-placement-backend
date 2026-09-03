import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ProjectBuildStage(Base):
    """One of the 10 fixed stages (app.services.project_builder.stages.STAGE_ORDER) of a
    ProjectBuild. guidance_text is generated once at build-creation time (AI-assisted, with a
    deterministic fallback) and stored -- it never changes underneath the student mid-build,
    even if they edit their notes later. notes is the student's own free text, genuinely
    mutable, and is what project_proof_service reads (as untrusted input) to ground the final
    Project->Proof output in what was actually built."""

    __tablename__ = "project_build_stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("project_builds.id"), nullable=False, index=True)
    stage_key: Mapped[str] = mapped_column(String(40), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    guidance_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("build_id", "stage_key", name="uq_project_build_stage"),)
