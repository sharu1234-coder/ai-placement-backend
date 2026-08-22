from typing import List, Optional

from pydantic import BaseModel

from app.ai.schemas.resume import (
    ResumeCertification,
    ResumeEducation,
    ResumeExperience,
    ResumeProject,
)


class ResumeAnalysisRead(BaseModel):
    id: int
    resume_id: int
    skills: List[str]
    education: List[ResumeEducation]
    experience: List[ResumeExperience]
    projects: List[ResumeProject]
    certifications: List[ResumeCertification]
    ats_score: float
    weak_sections: List[str]
    extraction_confidence: float


class ResumeUploadResponse(BaseModel):
    id: int
    parse_status: str
    analysis: Optional[ResumeAnalysisRead] = None
