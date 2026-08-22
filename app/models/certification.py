import datetime
from typing import Optional

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Certification(Base):
    __tablename__ = "certifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    issuer: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    issued_date: Mapped[Optional[datetime.date]] = mapped_column(Date, nullable=True)
