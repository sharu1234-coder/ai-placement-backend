from sqlalchemy import JSON, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ResumeAnalysis(Base):
    __tablename__ = "resume_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id"), unique=True, nullable=False)
    extracted_skills: Mapped[list] = mapped_column(JSON, nullable=False)
    extracted_education: Mapped[list] = mapped_column(JSON, nullable=False)
    extracted_experience: Mapped[list] = mapped_column(JSON, nullable=False)
    extracted_projects: Mapped[list] = mapped_column(JSON, nullable=False)
    extracted_certifications: Mapped[list] = mapped_column(JSON, nullable=False)
    ats_score: Mapped[float] = mapped_column(Float, nullable=False)
    weak_sections: Mapped[list] = mapped_column(JSON, nullable=False)
    extraction_confidence: Mapped[float] = mapped_column(Float, nullable=False)
