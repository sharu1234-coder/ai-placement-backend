from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    blueprint_id: Mapped[int] = mapped_column(ForeignKey("assessment_blueprints.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
