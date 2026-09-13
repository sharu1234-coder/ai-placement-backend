"""The OAuth state: who started an authorisation, and the proof that it is redeemed once.

Everything here is provider-agnostic. A provider adapter says "I need a state for this student",
gets back a raw state and a PKCE challenge to put in its authorization URL, and later asks for
the state to be consumed. No GitHub-specific knowledge lives in this module, which is what lets
a second provider reuse the same replay protection rather than growing its own.

Four properties, and the reason each is here rather than assumed:

`state` is 256 bits from `secrets`. It is the only thing standing between an attacker and
completing somebody's authorisation, so it is generated the same way a session token would be,
not from uuid4 or the clock.

Only the sha256 of the state is stored. The raw value travels through the system browser, a
redirect chain and, very likely, a proxy access log -- so it has to be treated as something an
attacker may already hold. Storing only the digest means a dump of this table cannot be used to
finish anyone's pending authorisation.

Consumption is a conditional UPDATE, not a read followed by a write. `WHERE consumed_at IS NULL`
with a rowcount check makes two simultaneous callbacks resolve to exactly one winner in the
database rather than in application code, where the check and the write can be interleaved. That
is the difference between single-use and almost-single-use.

The PKCE verifier never leaves the server. The device only opens a URL the backend built and
receives a status back; the verifier is generated here, encrypted with the same envelope as
provider tokens, and read back here. That is also why the redirect into the app can safely carry
nothing but a result -- there is no secret in the flow that the app is ever entitled to see.
"""

import base64
import datetime
import hashlib
import logging
import secrets
from typing import Tuple

from sqlalchemy.orm import Session

from app.core import token_crypto
from app.models.provider_oauth_state import ProviderOAuthState

logger = logging.getLogger(__name__)

# Long enough for the student to finish on GitHub (including a login and a 2FA prompt), short
# enough that a captured state is worth little. This is the window in which a stolen state has
# any value at all, and the student is in front of the screen for the whole of it.
STATE_TTL_MINUTES = 10

# 64 bytes of entropy renders to ~86 url-safe characters, inside RFC 7636's 43..128 range and
# drawn only from its unreserved alphabet.
_VERIFIER_ENTROPY_BYTES = 64
_STATE_ENTROPY_BYTES = 32


class OAuthStateError(Exception):
    """Base for every reason a callback's state is not usable.

    Subclassed so the server can record *which* failure happened, while the student is always
    shown the same generic result. Telling a caller apart -- "expired" versus "never existed"
    versus "already used" -- would turn this endpoint into an oracle for probing other people's
    pending authorisations.
    """

    log_reason = "invalid"


class UnknownStateError(OAuthStateError):
    log_reason = "unknown_state"


class ExpiredStateError(OAuthStateError):
    log_reason = "expired_state"


class ReplayedStateError(OAuthStateError):
    log_reason = "replayed_state"


class ProviderMismatchError(OAuthStateError):
    """The state was issued for a different provider than the callback that presented it.

    Not reachable today with one provider wired up, and deliberately implemented anyway: the
    moment LinkedIn is added, a state minted for one provider being redeemable at the other's
    callback would let a student's LinkedIn authorisation be spent creating a GitHub connection.
    Checking it now costs one comparison and means the second provider inherits the protection
    instead of needing to remember it.
    """

    log_reason = "provider_mismatch"


def hash_state(raw_state: str) -> str:
    """sha256 hex of the raw state. The only form that is ever persisted."""
    return hashlib.sha256(raw_state.encode("utf-8")).hexdigest()


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(_VERIFIER_ENTROPY_BYTES)


def code_challenge_for(verifier: str) -> str:
    """S256, per RFC 7636: base64url(sha256(verifier)) with padding stripped.

    GitHub documents S256 as the only accepted method -- `plain` is rejected -- so there is no
    method parameter here to get wrong, and callers cannot accidentally downgrade.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    """Normalise a value read back from the database before comparing it in Python.

    `expires_at` is timestamptz, so PostgreSQL returns it aware -- but SQLite has no timezone
    type at all and returns the same column naive, and comparing the two raises TypeError. That
    is not a theoretical asymmetry: it is what the expiry tests hit on SQLite while passing on
    PostgreSQL, on an expiry check whose whole job is to be right about time.

    Treating a naive value as UTC is correct here rather than merely convenient: everything
    written to this column comes from `_now()`, so UTC is the only thing a naive value can mean.
    Same helper, same reasoning, as evidence_engine._as_utc.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)


def create_state(db: Session, profile_id: int, provider: str) -> Tuple[str, str]:
    """Mint a pending authorisation. Returns (raw_state, code_challenge).

    The raw state is returned to the caller to be placed in the authorization URL and is never
    stored; the verifier is stored encrypted and never returned. The caller therefore cannot
    leak the verifier even by accident, because it never has it.
    """
    raw_state = secrets.token_urlsafe(_STATE_ENTROPY_BYTES)
    verifier = generate_code_verifier()

    db.add(
        ProviderOAuthState(
            state_hash=hash_state(raw_state),
            profile_id=profile_id,
            provider=provider,
            code_verifier_encrypted=token_crypto.encrypt(verifier),
            expires_at=_now() + datetime.timedelta(minutes=STATE_TTL_MINUTES),
        )
    )
    db.commit()

    # provider only -- never the state, the verifier, or the challenge.
    logger.info("provider=%s event=oauth_state_created", provider)
    return raw_state, code_challenge_for(verifier)


def consume_state(db: Session, raw_state: str, provider: str) -> Tuple[int, str]:
    """Redeem a state exactly once. Returns (profile_id, code_verifier).

    Called BEFORE the token exchange, deliberately. Consuming after a successful exchange would
    leave a window the length of a network round trip in which the same state could be redeemed
    again -- and a slow GitHub makes that window larger, which is precisely backwards. Marking
    it first means a replay arriving mid-exchange loses, and the cost of being strict is that a
    genuine network failure forces the student to press Connect again.
    """
    row = (
        db.query(ProviderOAuthState)
        .filter(ProviderOAuthState.state_hash == hash_state(raw_state))
        .one_or_none()
    )
    if row is None:
        raise UnknownStateError()
    if row.provider != provider:
        raise ProviderMismatchError()
    if row.consumed_at is not None:
        raise ReplayedStateError()
    if _as_utc(row.expires_at) <= _now():
        raise ExpiredStateError()

    # Atomic claim. The checks above give precise reasons for the log; this is what actually
    # decides the winner when two callbacks arrive together, because the UPDATE and the
    # predicate are evaluated by the database in one statement.
    claimed = (
        db.query(ProviderOAuthState)
        .filter(
            ProviderOAuthState.id == row.id,
            ProviderOAuthState.consumed_at.is_(None),
        )
        .update({"consumed_at": _now()}, synchronize_session=False)
    )
    db.commit()
    if claimed == 0:
        raise ReplayedStateError()

    verifier = token_crypto.decrypt(row.code_verifier_encrypted)
    logger.info("provider=%s event=oauth_state_consumed", provider)
    return row.profile_id, verifier


def delete_expired(db: Session) -> int:
    """Housekeeping. Rows outlive their usefulness by minutes; `expires_at` is indexed so this
    stays a range scan. Consumed rows are swept on expiry too -- they are kept past consumption
    only so a replay can be told apart from a state that never existed, and that distinction
    stops being useful once the state could not have been valid anyway."""
    removed = (
        db.query(ProviderOAuthState)
        .filter(ProviderOAuthState.expires_at <= _now())
        .delete(synchronize_session=False)
    )
    db.commit()
    return removed
