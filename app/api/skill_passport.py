from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.skill_passport import SkillPassportItemRead
from app.services import skill_passport_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/skill-passport", tags=["skill-passport"])


@router.get("", response_model=List[SkillPassportItemRead])
def list_skills(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[SkillPassportItemRead]:
    """Every skill in the profile's current role, each with its evidence-derived level,
    confidence, and status. Empty list (never fabricated placeholder skills) if no role is
    set or the role has no skills defined yet."""
    profile = get_or_create_profile(db, current_user)
    return skill_passport_service.list_skills(db, profile)


@router.get("/{skill}", response_model=SkillPassportItemRead)
def get_skill(
    skill: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SkillPassportItemRead:
    """The full evidence trail behind one skill -- the "WHY IS THIS VERIFIED?" view."""
    profile = get_or_create_profile(db, current_user)
    try:
        return skill_passport_service.get_skill(db, profile, skill)
    except skill_passport_service.SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
