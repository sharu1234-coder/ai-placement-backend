from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    jd_id: Mapped[int] = mapped_column(ForeignKey("job_descriptions.id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False)  # skill/tool/responsibility/experience/education
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    is_explicit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    importance: Mapped[str] = mapped_column(String(20), nullable=False, default="required")
