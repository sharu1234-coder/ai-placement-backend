from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.next_best_action import LOCKED_SECTION_ADDITIONAL_ACTIONS, NextBestActionRead
from app.services import next_best_action_service
from app.services.profile_service import get_or_create_profile
from app.services.subscription_service import is_premium

router = APIRouter(prefix="/next-best-action", tags=["next-best-action"])


@router.get("", response_model=NextBestActionRead)
def get_next_best_action(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NextBestActionRead:
    """The one thing this student should do next, with the real signals behind it.

    The top action stays free: what to work on next is the product's core promise, and a
    student who cannot see it has no reason to subscribe. Premium adds the rest of the ranked
    plan (`additional_actions`), gated here on the server rather than only in the client --
    PaywallGate hides UI, this is what stops a free account reading the full plan by calling
    the endpoint directly.

    `locked_sections` is populated only when there genuinely are further actions to unlock, so
    a student with one real recommendation is never shown an upgrade prompt for content that
    does not exist.
    """
    profile = get_or_create_profile(db, current_user)
    actions = next_best_action_service.get_actions(db, profile)
    primary, rest = actions[0], actions[1:]

    if is_premium(db, profile.id):
        return NextBestActionRead(**primary.model_dump(), additional_actions=rest)

    return NextBestActionRead(
        **primary.model_dump(),
        locked_sections=[LOCKED_SECTION_ADDITIONAL_ACTIONS] if rest else [],
    )
