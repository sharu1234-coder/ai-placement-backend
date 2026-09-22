import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.api.ai_errors import ai_unavailable
from app.api.deps import get_current_user, get_db
from app.api.jobs import JD_PARSE_LIMIT, JD_PARSE_SCOPE
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.career_goal import (
    CareerGoalAnalyzeResponse,
    CareerGoalCreate,
    CareerGoalDetailRead,
    CareerGoalListItem,
    CareerGoalUpdate,
)
from app.services import career_goal_service
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/career-goals", tags=["career-goals"])


@router.get("", response_model=List[CareerGoalListItem])
def list_career_goals(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[CareerGoalListItem]:
    """This profile's goals, most recently touched first. Runs no AI and no evidence pass --
    see career_goal_service._list_item for why the summary row stays cheap."""
    profile = get_or_create_profile(db, current_user)
    return career_goal_service.list_goals(db, profile.id)


@router.post("", response_model=CareerGoalListItem, status_code=status.HTTP_201_CREATED)
def create_career_goal(
    payload: CareerGoalCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerGoalListItem:
    """Save a target job. No AI runs here, so this cannot fail because a provider is down --
    the student's pasted job description is always kept and can be analyzed afterwards."""
    profile = get_or_create_profile(db, current_user)
    try:
        return career_goal_service.create(db, profile.id, payload)
    except career_goal_service.TooManyCareerGoalsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/{career_goal_id}", response_model=CareerGoalDetailRead)
def get_career_goal(
    career_goal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerGoalDetailRead:
    """The whole job-specific view: alignment, evidence, what needs attention, interview risks,
    the preparation plan, progress and the existing next best action.

    Deliberately not rate-limited and deliberately AI-free: everything is recomputed from rows
    that already exist, so opening this screen repeatedly costs nothing and is always current.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        return career_goal_service.get_detail(db, profile, career_goal_id)
    except career_goal_service.CareerGoalNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.patch("/{career_goal_id}", response_model=CareerGoalListItem)
def update_career_goal(
    career_goal_id: int,
    payload: CareerGoalUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerGoalListItem:
    """Edit a goal, or move it through active / paused / completed / archived.

    Archiving is a status change through this route rather than a separate endpoint: it is the
    same state machine as the rest, and a second way to reach it would be a second thing to
    keep in step.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        return career_goal_service.update(db, profile.id, career_goal_id, payload)
    except career_goal_service.CareerGoalNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.delete("/{career_goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_career_goal(
    career_goal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    profile = get_or_create_profile(db, current_user)
    try:
        career_goal_service.delete(db, profile.id, career_goal_id)
    except career_goal_service.CareerGoalNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{career_goal_id}/analyze", response_model=CareerGoalAnalyzeResponse)
# The same budget as parsing a job description anywhere else in this app, and the same counter:
# this IS that operation, reached through a different door. A separate limit here would hand
# the same student a second allowance of the identical AI call, which is exactly the doubling
# JD_PARSE_SCOPE was introduced to close between the paste and upload routes.
@limiter.shared_limit(JD_PARSE_LIMIT, scope=JD_PARSE_SCOPE)
def analyze_career_goal(
    request: Request,
    career_goal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> CareerGoalAnalyzeResponse:
    """Read the job description. The only AI call in this feature.

    Explicit rather than automatic on GET: analysis is the one part of a career goal that costs
    anything, so it happens when the student asks for it, not every time they open the screen.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        return career_goal_service.analyze(db, profile.id, career_goal_id, ai_provider)
    except career_goal_service.CareerGoalNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    # The goal is untouched by a provider failure: no partial parse is stored and the previous
    # analysis, if any, is still attached. ai_unavailable logs server-side and returns a fixed,
    # provider-agnostic message, so nothing about which model failed reaches the client.
    except AIGenerationError as exc:
        raise ai_unavailable(exc, logger, "career-goals")
