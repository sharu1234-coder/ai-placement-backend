import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# Every max_length below is its own column's cap, not a convention borrowed from a similarly
# shaped field elsewhere. Without one, an overlong value passes validation here and reaches
# PostgreSQL, which rejects it with StringDataRightTruncation -- an unhandled DataError, so the
# student gets a 500 instead of a 422 telling them what to shorten. SQLite does not enforce
# VARCHAR length at all, which is why the whole suite was green on it: the first run of these
# tests against PostgreSQL is what surfaced this.
#
# app/schemas/profile.py already carries this fix for StudentProfile's own fields and says so
# in the same words. These are the sub-resources the same pass missed.


class EducationCreate(BaseModel):
    institution: str = Field(max_length=255)
    degree: Optional[str] = Field(default=None, max_length=255)
    field_of_study: Optional[str] = Field(default=None, max_length=255)
    start_year: Optional[int] = None
    end_year: Optional[int] = None


class EducationRead(EducationCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class SkillCreate(BaseModel):
    name: str = Field(max_length=100)
    proficiency: Optional[str] = Field(default=None, max_length=50)


class SkillRead(SkillCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class ProjectCreate(BaseModel):
    # description stays at 2000 because its column is Text, and the cap is there for the AI
    # prompt it reaches verbatim (interview_service's question generation, via {projects})
    # rather than for storage. tech_stack was 300, copied from ProjectBuild.stack_summary,
    # whose column really is String(300) -- this one's column is String(255), so 256..300
    # passed validation and then 500'd on PostgreSQL.
    title: str = Field(max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    tech_stack: Optional[str] = Field(default=None, max_length=255)
    url: Optional[str] = Field(default=None, max_length=500)


class ProjectRead(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class ExperienceCreate(BaseModel):
    company: str = Field(max_length=255)
    title: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    start_date: Optional[datetime.date] = None
    end_date: Optional[datetime.date] = None


class ExperienceRead(ExperienceCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class CertificationCreate(BaseModel):
    name: str = Field(max_length=255)
    issuer: Optional[str] = Field(default=None, max_length=255)
    issued_date: Optional[datetime.date] = None


class CertificationRead(CertificationCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
