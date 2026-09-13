"""Connect, complete, report and disconnect -- the provider-agnostic half of the OAuth flow.

The one rule that shapes everything below: the student a connection is bound to comes from the
OAuth state row and from nowhere else. The callback arrives as a browser redirect from GitHub,
carrying no session, no bearer token and nothing else we issued except the state -- so any
identity taken from the request itself would be an identity an attacker chose. `complete()`
therefore takes no profile argument at all. It cannot bind the wrong student because it is never
told which student to bind.
"""

import datetime
import logging
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core import token_crypto
from app.models.provider_connection import (
    PROVIDERS,
    STATUS_ACTIVE,
    STATUS_REVOKED,
    SYNC_NEVER,
    ProviderConnection,
)
from app.services.professional_presence import oauth_state
from app.services.professional_presence.errors import ProviderError
from app.services.professional_presence.providers import github as github_provider

logger = logging.getLogger(__name__)


class ConnectionError_(Exception):
    """Base for connection-flow failures that are ours rather than the provider's."""


class UnknownProviderError(ConnectionError_):
    pass


class ProviderNotConfiguredError(ConnectionError_):
    """This deployment has no credentials for the provider.

    Surfaced before an authorization URL is ever offered, so the student sees "not available"
    on a screen they have not acted on -- rather than after authorising on GitHub's own site and
    being redirected into a failure they cannot do anything about.
    """


class IdentityAlreadyConnectedError(ConnectionError_):
    """This provider account is already live on a different student's profile.

    The database enforces this too (uq_provider_connection_active_identity), and the check here
    exists so the common case produces a clear result instead of an IntegrityError. The
    constraint remains the authority -- two simultaneous callbacks would both pass this check
    and exactly one would survive the insert.
    """


@dataclass(frozen=True)
class ConnectionStatus:
    """Everything the app is allowed to know about a connection.

    There is no token field, and that absence is the guarantee -- a response model that has
    nowhere to put a credential cannot leak one by a serialisation mistake later.
    """

    provider: str
    connected: bool
    username: Optional[str] = None
    status: Optional[str] = None
    sync_status: Optional[str] = None
    last_synced_at: Optional[datetime.datetime] = None
    last_error_kind: Optional[str] = None
    connected_at: Optional[datetime.datetime] = None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _adapter(provider_key: str):
    adapter = github_provider.get_provider(provider_key)
    if adapter is None:
        raise UnknownProviderError(provider_key)
    return adapter


def _live_connection(db: Session, profile_id: int, provider_key: str) -> Optional[ProviderConnection]:
    return (
        db.query(ProviderConnection)
        .filter(
            ProviderConnection.profile_id == profile_id,
            ProviderConnection.provider == provider_key,
            ProviderConnection.revoked_at.is_(None),
        )
        .one_or_none()
    )


def start(db: Session, profile_id: int, provider_key: str) -> str:
    """Mint a state and return the provider's authorization URL.

    The URL contains the raw state and is therefore not logged anywhere, by this function or
    any other -- an "authorization URL" line in a log aggregator is a credential in a log
    aggregator.
    """
    adapter = _adapter(provider_key)
    if not adapter.is_configured():
        raise ProviderNotConfiguredError(provider_key)
    if not token_crypto.is_configured():
        # Without encryption the PKCE verifier could not be stored, and storing it in the clear
        # is not an acceptable fallback. Refusing here keeps the failure in front of the student
        # rather than behind the provider's consent screen.
        raise ProviderNotConfiguredError(provider_key)

    raw_state, challenge = oauth_state.create_state(db, profile_id, provider_key)
    logger.info("provider=%s event=oauth_started", provider_key)
    return adapter.build_authorization_url(raw_state, challenge)


def complete(db: Session, provider_key: str, code: str, raw_state: str) -> ProviderConnection:
    """Redeem a callback into a stored connection.

    Ordering here is security-relevant and is not the order that reads most naturally:

    1. consume the state -- first, so a replay arriving during the network calls below loses;
    2. exchange the code, using the server-held verifier;
    3. ask the provider who the credential belongs to;
    4. refuse if that identity is already live on someone else's profile;
    5. retire the student's own previous connection, if any;
    6. store the new one, encrypted.

    Step 4 before step 5 matters: a student who mistakenly authorises an account that somebody
    else has already connected must be left exactly as they were, not disconnected as a side
    effect of a failed attempt.
    """
    adapter = _adapter(provider_key)
    profile_id, verifier = oauth_state.consume_state(db, raw_state, provider_key)

    credential = adapter.exchange_code(code, verifier)
    identity = adapter.fetch_identity(credential.access_token)

    clash = (
        db.query(ProviderConnection)
        .filter(
            ProviderConnection.provider == provider_key,
            ProviderConnection.provider_user_id == identity.provider_user_id,
            ProviderConnection.revoked_at.is_(None),
            ProviderConnection.profile_id != profile_id,
        )
        .first()
    )
    if clash is not None:
        logger.info("provider=%s event=oauth_rejected reason=identity_already_connected", provider_key)
        # The credential we just obtained is discarded rather than stored. Revoking it at the
        # provider as well would be tidier, but this is an account the requester may not own,
        # and silently ending its grant is not ours to do.
        raise IdentityAlreadyConnectedError()

    existing = _live_connection(db, profile_id, provider_key)
    if existing is not None:
        # Reconnect. The old row is retired rather than updated so `revoked_at` keeps meaning
        # "this grant ended here" instead of being overwritten, and so both partial unique
        # indexes free their slot for the new row.
        if existing.access_token_encrypted:
            try:
                adapter.revoke(token_crypto.decrypt(existing.access_token_encrypted))
            except token_crypto.TokenDecryptionFailed:
                # A rotated key. The old token cannot be revoked at the provider because it
                # cannot be read -- which is exactly why it is being replaced. Not a reason to
                # block the reconnect.
                logger.info("provider=%s event=revoke_skipped reason=undecryptable", provider_key)
        existing.revoked_at = _now()
        existing.status = STATUS_REVOKED
        existing.access_token_encrypted = None
        existing.refresh_token_encrypted = None
        db.flush()

    connection = ProviderConnection(
        profile_id=profile_id,
        provider=provider_key,
        provider_user_id=identity.provider_user_id,
        provider_username=identity.username,
        access_token_encrypted=token_crypto.encrypt(credential.access_token),
        refresh_token_encrypted=(
            token_crypto.encrypt(credential.refresh_token) if credential.refresh_token else None
        ),
        token_expires_at=credential.expires_at,
        granted_scopes=credential.granted_scopes,
        status=STATUS_ACTIVE,
        sync_status=SYNC_NEVER,
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)

    logger.info("provider=%s event=oauth_completed", provider_key)
    return connection


def status_for(db: Session, profile_id: int) -> List[ConnectionStatus]:
    """Every provider, connected or not -- the app's source of truth.

    Returning a row for providers that are NOT connected is deliberate. The browser flow can end
    without any callback at all (the student closes the tab, switches apps, loses signal), so the
    app cannot learn the outcome from the redirect and must be able to ask. A response that
    simply omits unconnected providers would make "not connected" and "unknown provider"
    indistinguishable to the client.
    """
    rows = {
        row.provider: row
        for row in db.query(ProviderConnection)
        .filter(
            ProviderConnection.profile_id == profile_id,
            ProviderConnection.revoked_at.is_(None),
        )
        .all()
    }

    statuses = []
    for provider_key in PROVIDERS:
        row = rows.get(provider_key)
        if row is None:
            statuses.append(ConnectionStatus(provider=provider_key, connected=False))
            continue
        statuses.append(
            ConnectionStatus(
                provider=provider_key,
                connected=True,
                username=row.provider_username,
                status=row.status,
                sync_status=row.sync_status,
                last_synced_at=row.last_synced_at,
                last_error_kind=row.last_error_kind,
                connected_at=row.created_at,
            )
        )
    return statuses


def disconnect(db: Session, profile_id: int, provider_key: str) -> bool:
    """Revoke at the provider where possible, then destroy our copy either way.

    Returns False when there was nothing to disconnect, so the router can answer 404 rather than
    reporting success for something that never happened.

    The row survives as the audit record that a grant once existed; what does not survive is the
    credential. Provider revocation is attempted first but never allowed to block: if GitHub is
    unreachable, the student is still disconnected here, because the alternative is a student who
    cannot disconnect during an outage.
    """
    connection = _live_connection(db, profile_id, provider_key)
    if connection is None:
        return False

    adapter = _adapter(provider_key)
    if connection.access_token_encrypted:
        try:
            adapter.revoke(token_crypto.decrypt(connection.access_token_encrypted))
        except token_crypto.TokenDecryptionFailed:
            logger.info("provider=%s event=revoke_skipped reason=undecryptable", provider_key)
        except ProviderError as exc:
            logger.info("provider=%s event=revoke_failed reason=%s", provider_key, exc.kind)

    connection.access_token_encrypted = None
    connection.refresh_token_encrypted = None
    connection.token_expires_at = None
    connection.revoked_at = _now()
    connection.status = STATUS_REVOKED
    db.commit()

    logger.info("provider=%s event=disconnected", provider_key)
    return True
