"""Connection endpoints for Professional Presence.

App Check is applied PER ROUTE here, not at the router, and that is the whole reason this file
is shaped the way it is. `/connections/{provider}/callback` is reached by the student's own
browser following a redirect from GitHub -- it carries no Firebase App Check header, no bearer
token and no session, because GitHub does not have any of those. Mounting App Check at the
router would 401 every successful authorisation.

That problem is not new and the codebase already solved it once: app/api/subscriptions.py owns
POST /subscriptions/webhook, called by Razorpay's servers, and handles it by leaving the router
unguarded and attaching `dependencies=_app_check_dep` to every route except the webhook. This
file follows that precedent exactly rather than inventing a second pattern, and app/main.py
registers it without router-level App Check for the same reason.

What replaces App Check on the callback is the state: 256 bits from `secrets`, stored only as a
hash, single-use, expiring, and bound to the student who began the flow. The callback never
reads an identity from the request -- `complete()` takes no profile argument -- so there is no
value an attacker can supply that changes whose account gets connected.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.app_check import verify_app_check_token
from app.core.config import settings
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.professional_presence import AuthorizationStartRead, ConnectionStatusRead
from app.services.professional_presence import connection_service, oauth_state
from app.services.professional_presence.errors import ProviderError
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/professional-presence", tags=["professional-presence"])

# Every route below EXCEPT the callback. See the module docstring.
_app_check_dep = [Depends(verify_app_check_token)]

# Result codes placed on the redirect back into the app. Coarse on purpose.
#
# Every state failure -- unknown, expired, already used, wrong provider -- collapses to
# `invalid_request`. Telling them apart in a response that anybody can trigger would turn this
# endpoint into an oracle for probing other people's pending authorisations: "expired" would
# confirm a state had once existed. The specific reason is logged server-side, where only we can
# read it.
_RESULT_INVALID = "invalid_request"
_RESULT_CANCELLED = "cancelled"
_RESULT_ALREADY_CONNECTED = "already_connected"
_RESULT_NOT_CONFIGURED = "not_configured"


def _app_redirect(provider: str, outcome: str, reason: Optional[str] = None) -> RedirectResponse:
    """Hand control back to the Android app carrying a RESULT and nothing else.

    No token, no refresh token, no authorization code, no state, no PKCE verifier. A custom
    scheme can be registered by any app on the device, so this link must be assumed readable by
    a hostile one -- and what such an app learns here is that some connection attempt succeeded
    or failed. The connection itself was already completed server-side and is bound to a student
    who authenticated with a bearer token this redirect never touches.

    This is also why the app cannot treat the redirect as authoritative: it re-reads
    GET /professional-presence/connections, which is authenticated, to learn what actually
    happened. The redirect is a convenience that closes the browser tab promptly.
    """
    query = f"provider={provider}&status={outcome}"
    if reason:
        query += f"&reason={reason}"
    return RedirectResponse(
        url=f"{settings.professional_presence_app_redirect}?{query}",
        status_code=status.HTTP_302_FOUND,
    )


@router.post(
    "/connections/{provider}/start",
    response_model=AuthorizationStartRead,
    dependencies=_app_check_dep,
)
@limiter.limit("10/hour")
def start_connection(
    request: Request,
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AuthorizationStartRead:
    """Begin an authorisation for the CALLING student.

    The profile comes from the bearer token via get_or_create_profile and is never accepted from
    the request body or a query parameter -- so a caller cannot start a flow that would bind a
    connection to somebody else's account.

    Rate limited because this is the endpoint with a cost: each call writes a state row. The
    callback deliberately is not (see there).
    """
    profile = get_or_create_profile(db, current_user)
    try:
        authorization_url = connection_service.start(db, profile.id, provider)
    except connection_service.UnknownProviderError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider.")
    except connection_service.ProviderNotConfiguredError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Connecting this provider is not available yet.",
        )
    return AuthorizationStartRead(
        authorization_url=authorization_url,
        expires_in_seconds=oauth_state.STATE_TTL_MINUTES * 60,
    )


@router.get("/connections/{provider}/callback", include_in_schema=False)
def oauth_callback(
    provider: str,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """GitHub redirects the student's browser here.

    Deliberately NOT behind App Check (GitHub's redirect carries no Firebase header) and NOT
    behind get_current_user (it carries no bearer token) -- the same treatment, for the same
    reason, that POST /subscriptions/webhook already gets for Razorpay.

    Deliberately NOT rate limited either. The limiter keys on IP, and a whole campus behind one
    NAT would share a budget for an endpoint they do not control the timing of. What makes that
    safe is that this route does no work for an unrecognised state: one indexed lookup on a
    256-bit unguessable value, then a redirect.

    Every failure ends in a redirect rather than an HTTP error. The student is in a browser tab
    at this point; a JSON 400 would strand them there, while a redirect returns them to the app
    where the status can actually be explained.
    """
    if error:
        # The student pressed Cancel on GitHub's consent screen, or GitHub declined. Not a
        # failure of ours, and not worth a scary message. `error` is provider text and is used
        # only to detect that something went wrong -- it is neither logged nor forwarded.
        logger.info("provider=%s event=oauth_callback_declined", provider)
        return _app_redirect(provider, _RESULT_CANCELLED)

    if not code or not state:
        logger.info("provider=%s event=oauth_callback_rejected reason=missing_parameters", provider)
        return _app_redirect(provider, "failed", _RESULT_INVALID)

    try:
        connection_service.complete(db, provider, code, state)
    except connection_service.UnknownProviderError:
        logger.info("provider=%s event=oauth_callback_rejected reason=unknown_provider", provider)
        return _app_redirect(provider, "failed", _RESULT_INVALID)
    except oauth_state.OAuthStateError as exc:
        # The precise reason is recorded here and nowhere the caller can see. exc.log_reason is
        # one of a fixed set of words, never a value from the request.
        logger.warning(
            "provider=%s event=oauth_callback_rejected reason=%s", provider, exc.log_reason
        )
        return _app_redirect(provider, "failed", _RESULT_INVALID)
    except connection_service.IdentityAlreadyConnectedError:
        return _app_redirect(provider, "failed", _RESULT_ALREADY_CONNECTED)
    except connection_service.ProviderNotConfiguredError:
        return _app_redirect(provider, "failed", _RESULT_NOT_CONFIGURED)
    except IntegrityError:
        # The partial unique indexes are the authority, and they win a race the pre-check above
        # cannot: two callbacks completing the same identity at once. Rolled back so the session
        # is usable -- on PostgreSQL a failed statement poisons the transaction until it is.
        db.rollback()
        logger.warning("provider=%s event=oauth_callback_rejected reason=identity_conflict", provider)
        return _app_redirect(provider, "failed", _RESULT_ALREADY_CONNECTED)
    except ProviderError as exc:
        # Category only. exc.kind is one of ProviderConnection.ERROR_KINDS; the provider's own
        # message is never logged or forwarded.
        logger.warning("provider=%s event=oauth_callback_failed reason=%s", provider, exc.kind)
        return _app_redirect(provider, "failed", exc.kind)

    return _app_redirect(provider, "connected")


@router.get("/connections", response_model=List[ConnectionStatusRead], dependencies=_app_check_dep)
def list_connections(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[ConnectionStatusRead]:
    """The app's source of truth for connection state.

    Needed because the browser flow can end without any callback at all: the student closes the
    tab, switches apps, or loses signal after approving. The app therefore never infers state
    from the redirect -- it asks here, on an authenticated request, whenever it resumes.
    """
    profile = get_or_create_profile(db, current_user)
    return [
        ConnectionStatusRead.model_validate(item)
        for item in connection_service.status_for(db, profile.id)
    ]


@router.delete(
    "/connections/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_app_check_dep,
)
def disconnect_provider(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Revoke at the provider where reachable, and destroy our copy of the credential either way.

    Scoped to the calling student's own profile, so this cannot be aimed at another account: the
    profile comes from the bearer token, and a provider the caller has not connected is a 404
    rather than a silent success.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        removed = connection_service.disconnect(db, profile.id, provider)
    except connection_service.UnknownProviderError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider.")
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not connected.")
