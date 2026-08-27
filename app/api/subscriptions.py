from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.subscription import SubscriptionStatusRead

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("/status", response_model=SubscriptionStatusRead)
def get_status(current_user: User = Depends(get_current_user)) -> SubscriptionStatusRead:
    """Every user is on the free tier -- there is no billing integration yet. Requires auth
    (not a public route like /legal) since it's meant to be read from a logged-in user's
    Subscriptions screen, not before signup."""
    return SubscriptionStatusRead(tier="free", status="coming_soon")
