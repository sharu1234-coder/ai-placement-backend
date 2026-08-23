import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    is_active: bool
    created_at: datetime.datetime


class AccountDeleteRequest(BaseModel):
    """Requires the current password so a merely-stolen/leaked access token can't silently
    trigger an irreversible account wipe -- deletion needs proof of the password too."""

    password: str
