from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.career_twin import CareerTwinRead, CareerTwinReadinessRead, SkillTwinItemRead
from app.services import career_twin_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/career-twin", tags=["career-twin"])


@router.get("", response_model=CareerTwinRead)
def get_career_twin(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerTwinRead:
    """The full Career Twin view: readiness, per-skill evidence breakdown, biggest gap, and
    the "why" behind the number. Sourced from the profile's current Career Path if one exists,
    falling back to their legacy target_role_id, and returning has_role=False (never a
    fabricated 0%) if neither is set."""
    profile = get_or_create_profile(db, current_user)
    return career_twin_service.get_current(db, profile)


@router.get("/readiness", response_model=CareerTwinReadinessRead)
def get_readiness(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerTwinReadinessRead:
    """Cheap projection for the Home screen's readiness card -- same computation as GET
    /career-twin, without the per-skill list."""
    profile = get_or_create_profile(db, current_user)
    twin = career_twin_service.get_current(db, profile)
    return CareerTwinReadinessRead(**twin.model_dump(exclude={"skills", "weak_skill_tags", "role_source"}))


@router.get("/skills", response_model=List[SkillTwinItemRead])
def get_skills(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[SkillTwinItemRead]:
    profile = get_or_create_profile(db, current_user)
    return career_twin_service.get_current(db, profile).skills


@router.get("/skills/{skill}", response_model=SkillTwinItemRead)
def get_skill(
    skill: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SkillTwinItemRead:
    """A thin, single-skill lookup over the same computation -- Skill Passport owns the
    deeper per-skill view (evidence item list, "why is this verified?"); this endpoint exists
    for a quick Career Twin drill-down without a separate round trip through Skill Passport."""
    profile = get_or_create_profile(db, current_user)
    twin = career_twin_service.get_current(db, profile)
    for item in twin.skills:
        if item.skill == skill:
            return item
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill {skill!r} not found in your Career Twin")
