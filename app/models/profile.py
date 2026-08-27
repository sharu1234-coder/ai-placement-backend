import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class StudentProfile(Base):
    __tablename__ = "student_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target_role_id: Mapped[Optional[int]] = mapped_column(ForeignKey("roles.id"), nullable=True)
    mobile_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    college: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    graduation_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    degree: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    experience_level: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    preparation_goals: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # A profile has either a catalog secondary role or nothing -- no custom-secondary
    # concept (see Phase 2 plan: custom role support is primary-role-only).
    secondary_target_role_id: Mapped[Optional[int]] = mapped_column(ForeignKey("roles.id"), nullable=True)
    # Mutually exclusive with target_role_id -- set only when the student's primary
    # target role isn't in the catalog. Enforced by the API layer, not a DB constraint.
    custom_target_role: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    onboarding_completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    @property
    def onboarding_completed(self) -> bool:
        """Plain Python property (not a mapped column) so StudentProfileRead's
        from_attributes=True serialization exposes the boolean the Android client
        needs, without leaking the raw timestamp into the API response."""
        return self.onboarding_completed_at is not None
