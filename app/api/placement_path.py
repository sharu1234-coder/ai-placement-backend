from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.placement_path import (
    PlacementPathRead,
    PlacementPathTopicRead,
    PracticeHandoffRead,
    UpdatePlacementPathTopicRequest,
)
from app.services import placement_path_service
from app.services.profile_service import get_or_create_profile
from app.services.skill_matching import NoResumeAnalysisError

router = APIRouter(prefix="/placement-path", tags=["placement-path"])


@router.post("/generate", response_model=PlacementPathRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
def generate(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> PlacementPathRead:
    """Creates the student's first placement path, or regenerates it (superseding the
    previous one, never deleting it) -- the same operation either way."""
    profile = get_or_create_profile(db, current_user)
    try:
        return placement_path_service.generate_placement_path(db, profile.id, ai_provider)
    except NoResumeAnalysisError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except placement_path_service.NoPlacementRelevantSkillsError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/current", response_model=Optional[PlacementPathRead])
def get_current(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Optional[PlacementPathRead]:
    profile = get_or_create_profile(db, current_user)
    return placement_path_service.get_current_placement_path(db, profile.id)


@router.get("/history", response_model=List[PlacementPathRead])
def get_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[PlacementPathRead]:
    """Every path this profile has ever had (active and superseded), most recent first --
    the "Saved Roadmaps" screen's data source."""
    profile = get_or_create_profile(db, current_user)
    return placement_path_service.get_placement_path_history(db, profile.id)


@router.post("/topics/{topic_id}/start", response_model=PlacementPathTopicRead)
def start_topic(
    topic_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlacementPathTopicRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return placement_path_service.start_topic(db, profile.id, topic_id)
    except placement_path_service.PlacementPathTopicNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except placement_path_service.TopicLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/topics/{topic_id}/practice", response_model=PracticeHandoffRead)
@limiter.limit("20/hour")
def practice(
    request: Request,
    topic_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PracticeHandoffRead:
    """Get-or-creates a single-skill AssessmentBlueprint scoped to this topic, then hands off
    entirely to the existing, unmodified assessment flow (POST /assessments {blueprint_id})."""
    profile = get_or_create_profile(db, current_user)
    try:
        blueprint_id = placement_path_service.get_or_create_practice_blueprint(db, profile.id, topic_id)
        return PracticeHandoffRead(blueprint_id=blueprint_id)
    except placement_path_service.PlacementPathTopicNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except placement_path_service.TopicLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except placement_path_service.NoAssessmentPathError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.patch("/topics/{topic_id}", response_model=PlacementPathTopicRead)
def update_topic(
    topic_id: int,
    payload: UpdatePlacementPathTopicRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlacementPathTopicRead:
    """The honest manual-completion fallback -- only for topics with no practice path (the
    checkpoint stages). Topics with a practice path can only be completed by passing the
    scoped assessment, enforced by the service layer."""
    profile = get_or_create_profile(db, current_user)
    try:
        return placement_path_service.mark_topic_complete_manual(db, profile.id, topic_id)
    except placement_path_service.PlacementPathTopicNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except placement_path_service.TopicLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except placement_path_service.ManualCompletionNotAllowedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
