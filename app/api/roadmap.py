from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.roadmap import GenerateRoadmapRequest, RoadmapRead, RoadmapTaskRead, UpdateRoadmapTaskRequest
from app.services import roadmap_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/roadmap", tags=["roadmap"])


@router.post("/generate", response_model=RoadmapRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
def generate_roadmap(
    request: Request,
    payload: GenerateRoadmapRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> RoadmapRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return roadmap_service.generate_roadmap(db, profile.id, payload.jd_id, payload.duration_days, ai_provider)
    except roadmap_service.InvalidRoadmapRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except roadmap_service.NoWeaknessDataError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except AIGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/current", response_model=Optional[RoadmapRead])
def get_current_roadmap(
    jd_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Optional[RoadmapRead]:
    """Not in PROJECT_HANDOFF.md's endpoint table, but needed for the Roadmap tab to load
    the student's active roadmap directly (rather than only ever via a nav-passed id right
    after generation) -- same kind of well-scoped addition as the audit's delete endpoints."""
    profile = get_or_create_profile(db, current_user)
    return roadmap_service.get_current_roadmap(db, profile.id, jd_id)


@router.patch("/tasks/{task_id}", response_model=RoadmapTaskRead)
def update_task(
    task_id: int,
    payload: UpdateRoadmapTaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RoadmapTaskRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return roadmap_service.update_task_status(db, profile.id, task_id, payload.status)
    except roadmap_service.RoadmapTaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{roadmap_id}/reassess", response_model=RoadmapRead)
@limiter.limit("10/hour")
def reassess(
    request: Request,
    roadmap_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> RoadmapRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return roadmap_service.reassess_roadmap(db, profile.id, roadmap_id, ai_provider)
    except roadmap_service.RoadmapNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except roadmap_service.NoWeaknessDataError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except AIGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
