from sqlalchemy import JSON, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class JobMatch(Base):
    __tablename__ = "job_matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    jd_id: Mapped[int] = mapped_column(ForeignKey("job_descriptions.id"), nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    matching_skills: Mapped[list] = mapped_column(JSON, nullable=False)
    missing_skills: Mapped[list] = mapped_column(JSON, nullable=False)
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
