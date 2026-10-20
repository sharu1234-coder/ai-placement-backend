"""Phase 1 of account deletion: revoke access and record the obligation, in one transaction.

The invariant this exists to hold, from docs/proposal-account-deletion-outbox.md section 1:
**never destroy the last pointer to an object that is still stored.** The previous design kept
it by deleting objects synchronously and abandoning the whole account deletion if storage was
unreachable -- correct, but it made a user's ability to leave depend on a bucket being up.

This design keeps the same invariant by writing the pointer down somewhere durable first. One
transaction:

1. read every storage key the profile owns;
2. insert a `deletion_requests` row and one `deletion_jobs` row per key;
3. revoke access -- bump `token_version`, clear `is_active`, drop provider credentials and
   reset tokens;
4. minimise the user row's directly identifying columns;
5. delete the profile-owned rows, including `resumes`;
6. commit.

Nothing here touches object storage or any other network service, so Phase 1 cannot fail
because storage is down -- and because the job insert and the resume delete are the same
transaction, a failure at any point leaves both the key and the object in place.

The user row itself survives this phase. Access was revoked in step 3, so its continued
existence grants nothing, and while it exists the request can be reasoned about operationally.
The worker deletes it after the last object is gone.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models.deletion_job import DeletionJob
from app.models.deletion_request import DeletionRequest
from app.models.resume import Resume
from app.models.user import User

logger = logging.getLogger(__name__)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def collect_storage_keys(db: Session, profile_id: int) -> list[tuple[str, str]]:
    """Every (provider, key) pair the profile owns, deduplicated.

    Read before anything is deleted, because afterwards there is nowhere left to read it from.
    Deduplicated here as well as by the unique constraint: two resume rows may legitimately
    reference one object, and that is one object to erase, not two jobs that race each other.

    `resumes.file_path` is the only profile-owned storage key in this schema today. When a
    second kind of object appears it is added here, and the parity test in
    tests/unit/test_deletion_outbox.py is what will notice if it is not.
    """
    from app.core.config import settings

    provider = settings.storage_provider
    keys = (
        db.query(Resume.file_path)
        .filter(Resume.profile_id == profile_id, Resume.file_path.isnot(None))
        .all()
    )
    seen: dict[str, str] = {}
    for (key,) in keys:
        if key and key not in seen:
            seen[key] = provider
    return [(provider_name, key) for key, provider_name in seen.items()]


def enqueue(db: Session, *, original_user_id: int, keys: list[tuple[str, str]]) -> DeletionRequest:
    """Insert the request and its jobs. Caller owns the transaction, deliberately.

    No commit and no flush-and-forget: this has to be part of the same unit of work as the row
    deletions, or the two can diverge. `db.flush()` is called only to obtain the request id for
    the job rows, which keeps everything inside the caller's transaction.
    """
    request = DeletionRequest(
        request_uuid=str(uuid.uuid4()),
        original_user_id=original_user_id,
        status="pending",
    )
    db.add(request)
    db.flush()

    now = _now()
    for provider, key in keys:
        db.add(
            DeletionJob(
                deletion_request_id=request.id,
                storage_provider=provider,
                storage_key=key,
                status="pending",
                attempts=0,
                # Claimable at once. The 5-minute pickup target is about how often the worker
                # looks, not about holding the job back.
                next_attempt_at=now,
            )
        )
    db.flush()
    return request


def revoke_access(db: Session, user: User) -> None:
    """Make every issued credential useless, without deleting the row.

    `token_version` is the mechanism: `app/core/security.py` embeds it in access and refresh
    tokens and the WebSocket handshake checks it, so incrementing it invalidates everything
    already issued, including sessions currently open. `is_active` false closes the ordinary
    login path as well.

    There is no `deleting` status column on `users` -- the proposal's schema is additive and
    alters no existing table, so the open `deletion_requests` row is the marker and these two
    existing columns do the revoking.
    """
    user.token_version = (user.token_version or 0) + 1
    user.is_active = False


def minimise(db: Session, user: User) -> None:
    """Remove directly identifying columns that the erasure itself does not need.

    The email is replaced rather than nulled because the column is NOT NULL and unique, and
    because a freed address should be reusable immediately -- a user who deletes their account
    and signs up again must not be blocked by their own tombstone. The replacement is derived
    from the request's own uuid space, not from the old address, so it is not reversible.

    The password hash is replaced with a value no password can produce. It is not nulled for
    the same NOT NULL reason.
    """
    user.email = f"deleted-{uuid.uuid4().hex}@deleted.invalid"
    user.password_hash = "!deleted"


def unresolved_request_for(db: Session, user_id: int) -> Optional[DeletionRequest]:
    """An open request for this user, if one exists. Makes a repeated request idempotent."""
    from app.models.deletion_request import UNRESOLVED_STATUSES

    return (
        db.query(DeletionRequest)
        .filter(
            DeletionRequest.original_user_id == user_id,
            DeletionRequest.status.in_(UNRESOLVED_STATUSES),
        )
        .first()
    )
