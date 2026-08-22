from typing import List, Optional

from pydantic import BaseModel


class ResumeEducation(BaseModel):
    institution: str
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None


class ResumeExperience(BaseModel):
    company: str
    title: Optional[str] = None
    description: Optional[str] = None


class ResumeProject(BaseModel):
    title: str
    description: Optional[str] = None
    tech_stack: Optional[str] = None


class ResumeCertification(BaseModel):
    name: str
    issuer: Optional[str] = None


class ParsedResume(BaseModel):
    skills: List[str]
    education: List[ResumeEducation]
    experience: List[ResumeExperience]
    projects: List[ResumeProject]
    certifications: List[ResumeCertification]
    ats_score: float
    weak_sections: List[str]
    extraction_confidence: float
    # every field must trace to resume text -- nothing fabricated (see prompt + AI rules)
