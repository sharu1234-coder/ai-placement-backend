from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.notification import AcknowledgeNotificationsRequest, PendingNotificationRead
from app.services import notification_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/pending", response_model=List[PendingNotificationRead])
def get_pending(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[PendingNotificationRead]:
    """Server-detected transitions not yet delivered to this student's device. There is no
    backend push-send path (see notification_service's module docstring) -- the client polls
    this on resume, shows/records each one locally, then calls POST /notifications/ack."""
    profile = get_or_create_profile(db, current_user)
    return notification_service.get_pending(db, profile.id)


@router.post("/ack", status_code=204)
@limiter.limit("120/hour")
def acknowledge(
    request: Request,
    payload: AcknowledgeNotificationsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Ownership-scoped by profile_id in the underlying UPDATE -- an id that doesn't belong to
    this profile is silently ignored, never a 403/404, since acking is purely "stop showing me
    this again" bookkeeping, not a resource the caller could leak information about."""
    profile = get_or_create_profile(db, current_user)
    notification_service.acknowledge(db, profile.id, payload.ids)
