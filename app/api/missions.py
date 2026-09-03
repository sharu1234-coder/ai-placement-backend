from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.mission import CompleteMissionRequest, MissionRead
from app.services import mission_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/missions", tags=["missions"])


@router.get("/today", response_model=MissionRead)
def get_today(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MissionRead:
    profile = get_or_create_profile(db, current_user)
    return mission_service.get_or_create_today(db, profile)


@router.post("/{mission_id}/start", response_model=MissionRead)
@limiter.limit("60/hour")
def start_mission(
    request: Request,
    mission_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MissionRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return mission_service.start(db, profile, mission_id)
    except mission_service.MissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{mission_id}/complete", response_model=MissionRead)
@limiter.limit("60/hour")
def complete_mission(
    request: Request,
    mission_id: int,
    payload: CompleteMissionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MissionRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return mission_service.complete(db, profile, mission_id, payload.assessment_id)
    except mission_service.MissionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except mission_service.AssessmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
