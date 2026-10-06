"""One row per logical mutating operation a student asked for, so a replay cannot repeat it.

Why this exists at all. The client cannot tell "my request never arrived" from "it arrived,
committed, and the reply was lost" -- a successful TCP write proves nothing about the far end.
Phase 24 tried to resolve that on the client by switching OkHttp's connection retry off for
mutating POSTs, and a live device run showed what that costs: the first tap after the phone had
been idle failed with "Couldn't reach the server" while the server's log showed the request had
never arrived at all. The retry was recovering from a dead pooled connection, correctly, and
turning it off broke the common case to guard the rare one.

So the retry stays, and the duplicate is collapsed here instead -- at the only place that knows
what actually happened.

**Why a generic table rather than one per domain.** `job_hunt_usages` already does this for Job
Hunt, and does it well, but its columns are about an *allowance* (`free_use_slot`, `was_premium`)
rather than about replay. Copying that shape into twenty-two domains would mean twenty-two
chances to get the savepoint handling subtly wrong. This table carries only what replay needs,
and every protected route reaches it through one service.

**What makes it correct is the unique constraint, not the code around it.**
`UNIQUE (profile_id, idempotency_key)` means two concurrent duplicates cannot both insert: one
wins and the other gets an IntegrityError, which is read as "my own duplicate got here first"
rather than as an error. There is no check-then-act window to lose, and the guarantee holds
across processes and workers because the storage engine enforces it. Python locks would not --
they are per-process, and this app runs more than one.

**Why the key alone is not enough.** A bare key is a re-entry token: send the same one with
different contents and you would be handed back the first operation's result for a request that
asked for something else. `request_fingerprint` is what closes that -- same key with different
contents is a conflict, never a silent substitution.

**What is deliberately not stored.** No request bodies, no response bodies, no headers, no
tokens. A resume upload's fingerprint is a hash of the file, never the file. The result is
recorded as a kind and a row id, and a replay re-reads that row and re-serialises it, so the
answer is reconstructed from the live record rather than from a cached copy that could go stale.
"""

import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

#: What a row is currently worth.
#:
#: claimed   -- the operation is running. A duplicate arriving now must not start a second one.
#: succeeded -- the operation produced a durable result, recorded in result_kind/result_id.
#:              A replay inside the window is answered from that row.
#: failed    -- the operation did not produce a result. A retry is a real retry and may proceed.
#: abandoned -- the claim outlived CLAIM_TIMEOUT without being settled, which in practice means
#:              the worker died mid-request. Reclaimable, for the same reason.
CLAIM_STATUSES = ("claimed", "succeeded", "failed", "abandoned")


class OperationClaim(Base):
    __tablename__ = "operation_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )

    #: Which logical operation this was, e.g. "assessment.start". Recorded for diagnostics, and
    #: mixed into the fingerprint so one key cannot be reused across two different operations.
    operation: Mapped[str] = mapped_column(String(60), nullable=False)

    #: Supplied by the client when it has one, minted server-side when it does not. A missing
    #: key is not an error: it simply means this request is its own operation and nothing can be
    #: replayed against it, which is the safe default for a client that forgets to send one.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    #: sha256 hex of the normalised, mutation-defining inputs. 64 chars exactly.
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="claimed", index=True
    )

    #: What the operation produced, as a kind and a row id -- never a serialised body. Null
    #: until the operation settles, and on every failure.
    result_kind: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    result_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.datetime.utcnow
    )
    #: Set only when the operation actually succeeded, so "when did this finish" has one answer.
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # The whole mechanism. Scoped to the owner, so two students may legitimately mint the
        # same key and neither can reach the other's row -- see the ownership test.
        UniqueConstraint("profile_id", "idempotency_key", name="uq_operation_claims_idempotency"),
        Index("ix_operation_claims_profile_status", "profile_id", "status"),
        # Retention sweeps by age; see idempotency.py's opportunistic cleanup.
        Index("ix_operation_claims_created_at", "created_at"),
    )
