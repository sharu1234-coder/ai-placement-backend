from typing import Optional

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class JobDescription(Base):
    __tablename__ = "job_descriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=True, index=True
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="paste")
    detected_role_id: Mapped[Optional[int]] = mapped_column(ForeignKey("roles.id"), nullable=True)
    role_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    parsing_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
