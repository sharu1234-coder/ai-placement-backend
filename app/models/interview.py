from typing import Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Interview(Base):
    __tablename__ = "interviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # technical/hr/project
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="text")
    jd_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job_descriptions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="in_progress")
