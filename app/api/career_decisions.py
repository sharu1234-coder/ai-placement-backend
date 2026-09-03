from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.career_decision import CareerDecisionSimulationRead, SimulateCareerDecisionsRequest
from app.services import career_decision_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/career-decisions", tags=["career-decisions"])


@router.post("/simulate", response_model=CareerDecisionSimulationRead)
@limiter.limit("30/hour")
def simulate(
    request: Request,
    payload: SimulateCareerDecisionsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerDecisionSimulationRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return career_decision_service.simulate(db, profile.id, payload.role_ids, payload.weekly_hours)
    except career_decision_service.InvalidRoleError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
