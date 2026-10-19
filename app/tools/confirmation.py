"""Confirmation grants: proof that a specific student approved a specific change.

A confirmation here is not a boolean. `confirmed=true` in a request body is worth nothing --
it is supplied by whoever is sending the request, which for this registry eventually means a
language model relaying a student's words. So what the server accepts is a token it minted
itself, bound to everything that made the preview true:

* the authenticated user, so a grant cannot be carried to another account,
* the exact capability name, so a grant for a save cannot execute an unsave,
* a fingerprint of the validated arguments, so changing an argument after the preview
  invalidates it,
* the resource identity, so a grant for one job cannot execute against another,
* an expiry, because approval of a state of affairs goes stale,
* single-use consumption, so a replayed confirmation cannot cause a second mutation.

**Why the token is split.** It reads `<confirmation_id>.<secret>`. The id is an index so the
record can be found in one lookup; the secret is compared in constant time against a stored
SHA-256 digest, so neither a timing difference nor a dump of this store yields a usable token.
Storing the token itself would mean a process memory dump handed out live approvals.

**Why the store is in-process, and what that costs.** This is 120 seconds of pre-execution
state, deliberately not a table and not a migration -- the same judgement VA-6 made about
memory and for the same reason: a durable store is a retention surface, and an approval that
outlives its screen is not safer for being persisted. The cost is real and is not hidden: with
more than one worker, a preview and its execute must be served by the same worker or the
execute is refused and the student must confirm again. The guarantee that genuinely has to
survive a worker -- "this mutation happens once" -- does not live here; it lives in the
existing `operation_claims` table, whose unique constraint is enforced by the database across
every worker. See docs/safe-actions.md.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from app.tools.contracts import RiskClass, ToolConfirmationInvalid

logger = logging.getLogger(__name__)

#: How long a preview stays actionable. Short on purpose: the preview asserted something about
#: live state ("this job is not saved"), and the longer the grant lives the less that sentence
#: is worth. Long enough to read a dialog and decide, not long enough to leave on a screen.
DEFAULT_TTL_SECONDS = 120.0

#: A ceiling so a client that previews in a loop cannot grow the worker.
MAX_PENDING = 256

#: And a ceiling per account, which is the part that makes the global one safe. Evicting the
#: globally oldest grant at the global cap would let one account that previews in a loop push
#: every other account's pending approval out of the store -- not a data leak, but a student
#: being told their own approval expired because somebody else was busy. So eviction always
#: takes from whoever is holding the most, and this quota is what makes "the most" meaningful.
#: Generous: a real client holds one pending approval, occasionally a few.
MAX_PENDING_PER_USER = 16

_SEPARATOR = "."


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfirmationGrant:
    """One pending approval. Everything the execution step must re-check is in here."""

    confirmation_id: str
    user_id: int
    capability: str
    argument_fingerprint: str
    resource_key: str
    resource_label: str
    description: str
    risk_class: RiskClass
    created_at: datetime.datetime
    expires_at: datetime.datetime
    #: Never the token, never the secret. See the module docstring.
    secret_digest: str

    def expires_in_seconds(self, now: Optional[datetime.datetime] = None) -> float:
        return max(0.0, (self.expires_at - (now or _utcnow())).total_seconds())

    def to_payload(self) -> dict[str, object]:
        """What the client is told about the grant. The token is returned separately, once, by
        `issue` -- it is never part of a describable record, so no later read of the store can
        hand it out again."""
        return {
            "confirmation_id": self.confirmation_id,
            "capability": self.capability,
            "description": self.description,
            "resource": self.resource_label,
            "risk_class": self.risk_class.value,
            "expires_in_seconds": round(self.expires_in_seconds(), 1),
        }


class ConfirmationStore:
    """Mints and consumes grants. Not a singleton: tests build their own."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_pending: int = MAX_PENDING,
        max_pending_per_user: int = MAX_PENDING_PER_USER,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("a confirmation needs a positive time to live")
        self.ttl_seconds = ttl_seconds
        self.max_pending = max_pending
        self.max_pending_per_user = max_pending_per_user
        self._grants: "OrderedDict[str, ConfirmationGrant]" = OrderedDict()
        # The registry is reachable from a request thread and from the WebSocket gateway's
        # thread pool, and consumption has to be single-use across both. A lock is what makes
        # "pop then act" atomic; without it two threads can both read the same live grant.
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)

    def issue(
        self,
        *,
        user_id: int,
        capability: str,
        argument_fingerprint: str,
        resource_key: str,
        resource_label: str,
        description: str,
        risk_class: RiskClass,
        now: Optional[datetime.datetime] = None,
    ) -> tuple[str, ConfirmationGrant]:
        """Mint a grant and return `(token, grant)`. The token is returned here and nowhere else."""
        moment = now or _utcnow()
        confirmation_id = secrets.token_urlsafe(12)
        # 32 bytes from the OS CSPRNG. Not a uuid4: a uuid is an identifier and is widely
        # assumed to be safe to log, and this value must never be logged.
        secret = secrets.token_urlsafe(32)
        grant = ConfirmationGrant(
            confirmation_id=confirmation_id,
            user_id=user_id,
            capability=capability,
            argument_fingerprint=argument_fingerprint,
            resource_key=resource_key,
            resource_label=resource_label,
            description=description,
            risk_class=risk_class,
            created_at=moment,
            expires_at=moment + datetime.timedelta(seconds=self.ttl_seconds),
            secret_digest=_digest(secret),
        )
        with self._lock:
            self._prune(moment)
            self._grants[confirmation_id] = grant
            self._enforce_quota_locked(user_id)
            while len(self._grants) > self.max_pending:
                self._evict_from_largest_holder_locked()
        # Identity and capability only. The token, the id and the arguments are all absent: an
        # id in a log is enough to correlate a grant with a student's activity, and the token
        # would be a live approval sitting in a log file.
        logger.info("confirmation issued for capability=%s user=%s", capability, user_id)
        return f"{confirmation_id}{_SEPARATOR}{secret}", grant

    def consume(
        self,
        token: Optional[str],
        *,
        user_id: int,
        capability: str,
        argument_fingerprint: str,
        now: Optional[datetime.datetime] = None,
    ) -> ConfirmationGrant:
        """Validate, burn and return the grant, or raise `ToolConfirmationInvalid`.

        Burned only on a complete match. A failed attempt leaves the grant alone on purpose:
        burning it would let a wrong-capability request invalidate a student's pending approval,
        turning a rejected call into a denial of their own action.
        """
        moment = now or _utcnow()
        if not token or _SEPARATOR not in token:
            raise ToolConfirmationInvalid(capability)
        confirmation_id, _, secret = token.partition(_SEPARATOR)

        with self._lock:
            self._prune(moment)
            grant = self._grants.get(confirmation_id)
            if grant is None:
                raise ToolConfirmationInvalid(capability)
            # Constant time, so a near-miss cannot be distinguished from a far one by how long
            # the comparison took. The digests are fixed length, which is what makes this
            # comparison meaningful rather than theatre.
            if not hmac.compare_digest(grant.secret_digest, _digest(secret)):
                raise ToolConfirmationInvalid(capability)
            # Every binding re-checked. Each of these is a separate way a valid token could be
            # pointed at something it was never approved for.
            if grant.user_id != user_id:
                logger.warning("confirmation %s presented by a different account", "<redacted>")
                raise ToolConfirmationInvalid(capability)
            if grant.capability != capability:
                raise ToolConfirmationInvalid(capability)
            if not hmac.compare_digest(grant.argument_fingerprint, argument_fingerprint):
                raise ToolConfirmationInvalid(capability)
            if grant.expires_at <= moment:
                self._grants.pop(confirmation_id, None)
                raise ToolConfirmationInvalid(capability)
            # Single use. Removed before the caller does anything with it, so a second request
            # racing this one finds nothing -- the lock is what makes that true rather than
            # likely.
            self._grants.pop(confirmation_id, None)
        return grant

    def peek(self, confirmation_id: str) -> Optional[ConfirmationGrant]:
        """For tests and diagnostics. Returns the record, which never contains the token."""
        with self._lock:
            return self._grants.get(confirmation_id)

    def reset(self) -> None:
        with self._lock:
            self._grants.clear()

    def _prune(self, now: datetime.datetime) -> None:
        expired = [key for key, grant in self._grants.items() if grant.expires_at <= now]
        for key in expired:
            self._grants.pop(key, None)

    def _enforce_quota_locked(self, user_id: int) -> None:
        """Drop this account's oldest grants until it is inside its own quota.

        Its own, and nobody else's: whichever account is minting grants pays for it.
        """
        mine = [key for key, grant in self._grants.items() if grant.user_id == user_id]
        for key in mine[: max(0, len(mine) - self.max_pending_per_user)]:
            self._grants.pop(key, None)

    def _evict_from_largest_holder_locked(self) -> None:
        """Make room at the global cap by taking from whoever holds the most.

        The backstop for the case the per-user quota cannot reach: enough separate accounts
        each inside their quota to fill the store. Taking from the largest holder means a
        student with one pending approval keeps it while a flooder loses one, which is the
        opposite of what evicting the globally oldest entry would have done.
        """
        counts: dict[int, int] = {}
        for grant in self._grants.values():
            counts[grant.user_id] = counts.get(grant.user_id, 0) + 1
        if not counts:  # pragma: no cover - only reachable with an empty store
            return
        worst = max(counts, key=lambda uid: counts[uid])
        # The key is resolved before anything is removed, rather than popping mid-iteration.
        oldest = next((key for key, grant in self._grants.items() if grant.user_id == worst), None)
        if oldest is not None:
            self._grants.pop(oldest, None)


#: The process-wide store the API routes use. Module-level for the same reason the VA-6 memory
#: store is: one per process, replaceable in a test by constructing another.
store = ConfirmationStore()
