import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class EducationCreate(BaseModel):
    institution: str
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None


class EducationRead(EducationCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class SkillCreate(BaseModel):
    name: str
    proficiency: Optional[str] = None


class SkillRead(SkillCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class ProjectCreate(BaseModel):
    title: str
    description: Optional[str] = None
    tech_stack: Optional[str] = None
    url: Optional[str] = None


class ProjectRead(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class ExperienceCreate(BaseModel):
    company: str
    title: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[datetime.date] = None
    end_date: Optional[datetime.date] = None


class ExperienceRead(ExperienceCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class CertificationCreate(BaseModel):
    name: str
    issuer: Optional[str] = None
    issued_date: Optional[datetime.date] = None


class CertificationRead(CertificationCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
