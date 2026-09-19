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

import datetime
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.core.app_check import verify_app_check_token
from app.core.config import settings
from app.core.rate_limit import limiter
from app.models.external_evidence_item import ExternalEvidenceItem
from app.models.external_profile_snapshot import ExternalProfileSnapshot
from app.models.provider_connection import PROVIDER_GITHUB
from app.models.user import User
from app.schemas.professional_presence import (
    AuditFindingRead,
    AuthorizationStartRead,
    ConnectionStatusRead,
    PresenceAuditRead,
    PresenceHistoryItemRead,
    SyncResultRead,
)
from app.services.professional_presence import (
    audit_service,
    connection_service,
    oauth_state,
    sync_service,
)
from app.services.professional_presence.errors import ProviderError, ProviderUnauthorized
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

# History is a "what changed?" screen, not an archive browser. A cap keeps one response from
# growing without bound for a student who has synced for years.
_MAX_HISTORY_ITEMS = 24

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

# The one answer to "this connection cannot be synced until the student reconnects", whichever
# way it was reached: an unreadable stored credential, or GitHub rejecting the credential itself.
_RECONNECT_REQUIRED = "This connection needs to be reconnected before it can be synced."


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


@router.post(
    "/connections/{provider}/sync",
    response_model=SyncResultRead,
    dependencies=_app_check_dep,
)
@limiter.limit("6/hour")
def sync_connection(
    request: Request,
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> SyncResultRead:
    """Re-read the connected account and store what it establishes.

    Explicitly student-triggered. There is no background scheduler in this deployment, and the
    app deliberately does not sync on every screen render -- a sync spends the student's GitHub
    quota and, when content has changed, one AI generation. Both are real costs that should
    follow an intent rather than a navigation event.

    6/hour keyed by the authenticated student (app/core/rate_limit.user_or_ip_key), which is a
    per-worker ceiling rather than an exact global quota -- see docs/PRODUCTION_DEPLOYMENT.md.
    It is generous for the honest case, since a student has no reason to re-read an account they
    have not changed, and the fingerprint check means an unchanged account costs no AI call at
    all.

    A provider failure is a 502 rather than a 500: the fault is upstream, the category is already
    recorded on the connection, and the message the student sees is written by us. Nothing about
    the provider's own error text travels.

    The exception is a credential GitHub rejects, which is a 409 and not a 502. By then the sync
    has marked the connection `needs_reauth`, so it is the same state the 409 above reports, and
    it needs the same instruction: reconnect. Answering it with the 502 that means "GitHub could
    not be reached" told the student to wait for something waiting would never fix, beside a
    connection card correctly saying the opposite.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        result = sync_service.sync(db, profile.id, provider, ai_provider=ai_provider)
    except sync_service.ConnectionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not connected.")
    except sync_service.ConnectionNotSyncableError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_RECONNECT_REQUIRED)
    except ProviderUnauthorized as exc:
        logger.warning("provider=%s event=sync_failed reason=%s", provider, exc.kind)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_RECONNECT_REQUIRED)
    except ProviderError as exc:
        logger.warning("provider=%s event=sync_failed reason=%s", provider, exc.kind)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    return SyncResultRead(
        provider=result.provider,
        evidence_added=result.evidence_added,
        evidence_removed=result.evidence_removed,
        repositories_seen=result.repositories_seen,
        snapshot_written=result.snapshot_written,
        complete=result.complete,
    )


# Freshness thresholds, applied server-side so every client says the same thing about the same
# age. Matches the Evidence Engine's 90-day staleness window at the far end so the two features
# cannot describe one repository differently.
_FRESH_DAYS = 7
_AGING_DAYS = 90


def _freshness(last_synced_at: Optional[datetime.datetime]) -> str:
    if last_synced_at is None:
        return "never"
    observed = last_synced_at if last_synced_at.tzinfo else last_synced_at.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc) - observed).days
    if age <= _FRESH_DAYS:
        return "fresh"
    if age <= _AGING_DAYS:
        return "aging"
    return "stale"


def _counted_repositories(payload: dict) -> List[dict]:
    """The repositories a snapshot describes, excluding forks and archives.

    One definition, used by both `/audit`'s `repository_count` and `/history`'s
    `public_repositories`, because they are the same claim and a screen may show them side by
    side. It is also the set that produces evidence (sync_service._is_evidence_bearing):
    counting forks here would let a student see "20 repositories, 3 evidence" and reasonably
    conclude the sync is broken, when the truth is that seventeen of them are somebody else's
    code.
    """
    repos = [r for r in (payload.get("repositories") or []) if isinstance(r, dict)]
    return [r for r in repos if not r.get("is_fork") and not r.get("is_archived")]


def _evidence_count(db: Session, connection_id: int) -> int:
    """How many evidence rows this connection currently holds.

    Counted in SQL rather than loaded and len()'d: this is rendered on a screen that opens
    often, and the rows themselves are never wanted here. Scoped to the connection, so a
    revoked grant's leftovers can never inflate a live connection's number.
    """
    return (
        db.query(func.count(ExternalEvidenceItem.id))
        .filter(ExternalEvidenceItem.connection_id == connection_id)
        .scalar()
        or 0
    )


@router.get("/audit", response_model=PresenceAuditRead, dependencies=_app_check_dep)
def get_presence_audit(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PresenceAuditRead:
    """The audit for the student's GitHub connection, computed from stored data only.

    Calls no provider and no AI. Observations are recomputed here rather than stored because
    half of them compare GitHub against the student's own declared skills and resume, which
    change without GitHub changing -- a stored audit would go quietly wrong the moment a student
    added a skill. The generated prose *is* stored, keyed by observation, and is looked up rather
    than regenerated; a finding with no stored prose falls back to a hand-written action.

    Returns a well-formed body for every state, including not-connected and never-synced. A
    client must be able to render "connect GitHub" and "GitHub could not be reached" from the
    same response shape, and an endpoint that 404s on "no data yet" makes those two look alike.
    """
    profile = get_or_create_profile(db, current_user)
    connection = sync_service.live_connection(db, profile.id, PROVIDER_GITHUB)

    if connection is None:
        return PresenceAuditRead(provider=PROVIDER_GITHUB, connected=False, freshness="never")

    snapshot = sync_service.latest_snapshot(db, connection.id)
    base = dict(
        provider=PROVIDER_GITHUB,
        connected=True,
        username=connection.provider_username,
        status=connection.status,
        sync_status=connection.sync_status,
        last_synced_at=connection.last_synced_at,
        last_error_kind=connection.last_error_kind,
        freshness=_freshness(connection.last_synced_at),
    )
    if snapshot is None:
        # Connected but never successfully synced. Deliberately not an error: the student has
        # done their part, and the screen's job is to offer the sync rather than to report a fault.
        # Both counts stay at their zero default, which is the truth rather than a placeholder.
        return PresenceAuditRead(**base)

    payload = snapshot.payload if isinstance(snapshot.payload, dict) else {}
    narrative = payload.get("narrative") if isinstance(payload.get("narrative"), dict) else {}
    findings = audit_service.assemble(
        audit_service.observations_for(db, profile.id, snapshot), narrative
    )
    return PresenceAuditRead(
        **base,
        snapshot_captured_at=snapshot.created_at,
        listing_complete=bool(payload.get("listing_complete", True)),
        repository_count=len(_counted_repositories(payload)),
        evidence_count=_evidence_count(db, connection.id),
        findings=[
            AuditFindingRead(
                code=f.code,
                kind=f.kind,
                subject=f.subject,
                evidence=f.evidence,
                why_it_matters=f.why_it_matters,
                recommended_action=f.recommended_action,
                is_template=f.is_template,
            )
            for f in findings
        ],
        narrative_prompt_version=payload.get("narrative_prompt_version"),
    )


@router.get("/history", response_model=List[PresenceHistoryItemRead], dependencies=_app_check_dep)
def get_presence_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[PresenceHistoryItemRead]:
    """Past states of the account, newest first, so "what has changed?" has a real answer.

    Only snapshots for the student's own live connection: a revoked grant's history describes an
    authorisation that no longer exists, and presenting it as continuous would imply a continuity
    that was broken.
    """
    profile = get_or_create_profile(db, current_user)
    connection = sync_service.live_connection(db, profile.id, PROVIDER_GITHUB)
    if connection is None:
        return []

    rows = (
        db.query(ExternalProfileSnapshot)
        .filter(ExternalProfileSnapshot.connection_id == connection.id)
        .order_by(ExternalProfileSnapshot.sequence.desc())
        .limit(_MAX_HISTORY_ITEMS)
        .all()
    )

    items = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        own = _counted_repositories(payload)
        languages = sorted(
            {
                language
                for repo in own
                for language in (repo.get("languages") or [])
                if isinstance(language, str)
            }
        )
        items.append(
            PresenceHistoryItemRead(
                sequence=row.sequence,
                captured_at=row.created_at,
                public_repositories=len(own),
                described_repositories=len([r for r in own if r.get("description")]),
                languages=languages,
            )
        )
    return items


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
