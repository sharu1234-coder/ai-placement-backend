import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


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
    # max_length values match this app's other free-text project/AI-input fields for the
    # same shape of content (see app/schemas/project_builder.py's ProjectBuild title/
    # stack_summary/repo_url caps) -- description/tech_stack in particular were previously
    # completely unbounded despite reaching an AI prompt verbatim (interview_service's
    # question-generation prompt, via {projects}), a real resource-exhaustion/AI-cost vector
    # every other large free-text AI input in the app already had a cap against.
    title: str = Field(max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    tech_stack: Optional[str] = Field(default=None, max_length=300)
    url: Optional[str] = Field(default=None, max_length=500)


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
