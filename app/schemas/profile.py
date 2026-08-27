from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

ExperienceLevel = Literal["beginner", "intermediate", "advanced"]


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: Optional[str]
    description: Optional[str]


class RoleSelectRequest(BaseModel):
    """Exactly one of role_id/custom_role must be set -- a catalog pick or a free-text
    role the student typed in, never both, never neither."""

    role_id: Optional[int] = None
    # max_length matches StudentProfile.custom_target_role's String(150) column -- without
    # this, an overlong value passes validation here and fails as an unhandled DB error on
    # Postgres (production) instead of a clean 422.
    custom_role: Optional[str] = Field(default=None, max_length=150)

    @model_validator(mode="after")
    def _exactly_one(self) -> "RoleSelectRequest":
        if (self.role_id is None) == (self.custom_role is None):
            raise ValueError("Provide exactly one of role_id or custom_role")
        return self


class SecondaryRoleSelectRequest(BaseModel):
    role_id: int


class StudentProfileUpsert(BaseModel):
    # max_length on each matches its StudentProfile column's String(255) cap -- otherwise an
    # overlong value passes validation here and fails as an unhandled DB error on Postgres
    # (production) instead of a clean 422.
    full_name: Optional[str] = Field(default=None, max_length=255)
    mobile_number: Optional[str] = Field(default=None, max_length=20)
    college: Optional[str] = Field(default=None, max_length=255)
    graduation_year: Optional[int] = None
    degree: Optional[str] = Field(default=None, max_length=255)
    branch: Optional[str] = Field(default=None, max_length=255)
    experience_level: Optional[ExperienceLevel] = None
    preparation_goals: Optional[List[str]] = None


class StudentProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: Optional[str]
    # Not a StudentProfile column -- it's the User's login identity. Populated explicitly
    # by app.services.profile_service.profile_read(), never via bare from_attributes=True.
    email: str
    mobile_number: Optional[str]
    target_role_id: Optional[int]
    secondary_target_role_id: Optional[int]
    custom_target_role: Optional[str]
    college: Optional[str]
    graduation_year: Optional[int]
    degree: Optional[str]
    branch: Optional[str]
    experience_level: Optional[str]
    preparation_goals: Optional[List[str]]
    # StudentProfile stores a timestamp (onboarding_completed_at); the client only
    # needs the boolean, exposed via a plain @property on the model (see profile.py)
    # so from_attributes=True picks it up like any other field -- no raw timestamp
    # leaks into the response.
    onboarding_completed: bool
