from typing import Optional

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class AssessmentBlueprint(Base):
    __tablename__ = "assessment_blueprints"

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[Optional[int]] = mapped_column(ForeignKey("roles.id"), nullable=True)
    jd_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job_descriptions.id"), nullable=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="default_taxonomy")
    sections: Mapped[list] = mapped_column(JSON, nullable=False)
