from typing import Optional

from pydantic import BaseModel, ConfigDict


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: Optional[str]
    description: Optional[str]


class RoleSelectRequest(BaseModel):
    role_id: int


class StudentProfileUpsert(BaseModel):
    full_name: Optional[str] = None
    college: Optional[str] = None
    graduation_year: Optional[int] = None


class StudentProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: Optional[str]
    target_role_id: Optional[int]
    college: Optional[str]
    graduation_year: Optional[int]
