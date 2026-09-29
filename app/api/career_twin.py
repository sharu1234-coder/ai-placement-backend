from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.career_twin import CareerTwinRead, CareerTwinReadinessRead, SkillTwinItemRead
from app.services import career_twin_service
from app.services.profile_service import get_or_create_profile
from app.services.subscription_service import is_premium

router = APIRouter(prefix="/career-twin", tags=["career-twin"])

# The "Why X%?" breakdown is the one Premium-gated part of an otherwise-free Career Twin
# screen (see ui/careertwin/CareerTwinScreen.kt's PaywallGate around this exact field) -- a
# non-empty placeholder, not [], so the client's own `if (reasons.isNotEmpty())` check still
# shows the upgrade prompt instead of silently hiding the section for free users.
_LOCKED_REASONS = ["Upgrade to Premium to see the full evidence breakdown behind your readiness score."]


def _gate_reasons(twin: CareerTwinRead, db: Session, profile_id: int) -> CareerTwinRead:
    if twin.reasons and not is_premium(db, profile_id):
        return twin.model_copy(update={"reasons": _LOCKED_REASONS})
    return twin


@router.get("", response_model=CareerTwinRead)
def get_career_twin(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerTwinRead:
    """The full Career Twin view: evidence coverage, per-skill evidence breakdown, biggest gap,
    the "why" behind the number. Sourced from the profile's current Career Path if one exists,
    falling back to their legacy target_role_id, and returning has_role=False (never a
    fabricated 0%) if neither is set. `reasons` is the one field this endpoint gates server-side
    -- everything else here is the free baseline."""
    profile = get_or_create_profile(db, current_user)
    twin = career_twin_service.get_current(db, profile)
    return _gate_reasons(twin, db, profile.id)


@router.get("/readiness", response_model=CareerTwinReadinessRead)
def get_readiness(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerTwinReadinessRead:
    """Cheap projection for the Home screen's evidence-coverage card -- same computation as GET
    /career-twin, without the per-skill list."""
    profile = get_or_create_profile(db, current_user)
    twin = career_twin_service.get_current(db, profile)
    twin = _gate_reasons(twin, db, profile.id)
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
