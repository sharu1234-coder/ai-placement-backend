import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.placement_path import (
    PlacementPathRead,
    PlacementPathTopicRead,
    PracticeHandoffRead,
    UpdatePlacementPathTopicRequest,
)
from app.services import idempotency, placement_path_service
from app.services.profile_service import get_or_create_profile
from app.services.skill_matching import NoResumeAnalysisError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/placement-path", tags=["placement-path"])


@router.post("/generate", response_model=PlacementPathRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
def generate(
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> PlacementPathRead:
    """Creates the student's first placement path, or regenerates it (superseding the
    previous one, never deleting it) -- the same operation either way."""
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[PlacementPathRead, int]:
        try:
            # ensure_placement_path, not generate_placement_path. The claim around this call
            # is keyed on the client's Idempotency-Key and so only ever collapses one request's
            # own retransmissions; the business guard inside it is keyed on the profile and is
            # what stops two *different* requests from both paying for a 163-second generation
            # and both leaving an active path behind.
            created = placement_path_service.ensure_placement_path(db, profile.id, ai_provider)
        except NoResumeAnalysisError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except placement_path_service.NoPlacementRelevantSkillsError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        return created, created.id

    try:
        # The longest AI call in the app -- ~163s measured against a local model -- and it
        # supersedes the student's current path, so a replay is both a wasted generation and a
        # second path retiring the first.
        return idempotency.run_once(
            db,
            profile.id,
            operation=placement_path_service.PLACEMENT_PATH_OPERATION,
            idempotency_key=idempotency_key,
            params={},
            perform=perform,
            read_back=lambda rid: placement_path_service.read_placement_path(db, profile.id, rid),
            result_kind="placement_path",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(
            exc, logger, placement_path_service.PLACEMENT_PATH_OPERATION
        )


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
