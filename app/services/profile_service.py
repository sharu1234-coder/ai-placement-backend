from sqlalchemy.orm import Session

from app.models.profile import StudentProfile
from app.models.user import User
from app.schemas.profile import StudentProfileRead


def get_or_create_profile(db: Session, user: User) -> StudentProfile:
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    if profile is None:
        profile = StudentProfile(user_id=user.id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def profile_read(profile: StudentProfile, user: User) -> StudentProfileRead:
    """email lives on User, not StudentProfile -- every route returning StudentProfileRead
    must go through this instead of relying on bare from_attributes=True. Built field-by-
    field rather than model_validate(profile, from_attributes=True).model_copy(update=...):
    email is a required field with no ORM attribute to read, so validation would fail
    before model_copy ever got a chance to inject it."""
    return StudentProfileRead(
        id=profile.id,
        full_name=profile.full_name,
        email=user.email,
        mobile_number=profile.mobile_number,
        target_role_id=profile.target_role_id,
        secondary_target_role_id=profile.secondary_target_role_id,
        custom_target_role=profile.custom_target_role,
        college=profile.college,
        graduation_year=profile.graduation_year,
        degree=profile.degree,
        branch=profile.branch,
        experience_level=profile.experience_level,
        preparation_goals=profile.preparation_goals,
        onboarding_completed=profile.onboarding_completed,
    )
