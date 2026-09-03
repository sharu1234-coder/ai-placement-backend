from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.next_best_action import NextBestActionRead
from app.services import next_best_action_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/next-best-action", tags=["next-best-action"])


@router.get("", response_model=NextBestActionRead)
def get_next_best_action(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NextBestActionRead:
    """The one thing this student should do next, with the real signals behind it."""
    profile = get_or_create_profile(db, current_user)
    return next_best_action_service.get_next_best_action(db, profile)
