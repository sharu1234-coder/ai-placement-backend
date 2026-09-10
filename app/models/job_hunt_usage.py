"""One row per Job Hunt operation a student actually asked for.

Why a usage *ledger* rather than a `job_hunt_used = true` flag on the profile.

A boolean cannot answer any of the four questions this feature actually has to answer. It
cannot say *when* the free search was spent, so a support request ("I never used it") has no
evidence either way. It cannot make two simultaneous requests resolve to one consumption,
because "read the flag, then set it" is a race no amount of care in Python closes. It cannot
recognise a retry of the same operation, so a network timeout on the student's side costs them
their one search. And it cannot survive the product growing a second tier, because there is
nowhere to put "which allowance did this spend".

So each attempt is a row, and the *database* decides who wins.

`free_use_slot` is the whole enforcement mechanism, and it is worth being precise about. It
holds either 1 or NULL, under `UNIQUE (profile_id, free_use_slot)`. Both PostgreSQL and SQLite
treat NULLs in a unique constraint as distinct from one another, so a profile may have any
number of NULL rows -- every premium search, every released failure -- but at most one row
where the value is 1. Claiming the free search *is* inserting that row; being refused *is*
the constraint rejecting the second insert. There is no check-then-act window to lose, and the
guarantee holds across processes and workers because it is the storage engine enforcing it,
not application code.

Releasing a claim (the provider was down, so the student should not have been charged for it)
sets the slot back to NULL and leaves the row in place with status "failed". The attempt stays
in the ledger -- that is the point of a ledger -- but it no longer occupies the allowance.

`request_fingerprint` is what stops the idempotency key from becoming the bypass. A key on its
own is a re-entry token: send the same one forever and every search is free. So a reused key
must carry the same search, and the fingerprint is how that is checked. Changing the role, the
location or any filter changes the fingerprint, and a key reused across a different fingerprint
is a conflict rather than a free ride.
"""

import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

#: What a row is currently worth.
#:
#: claimed   -- the allowance is spoken for and the operation is running. Holds the slot.
#: succeeded -- the operation produced a real result. Holds the slot permanently.
#: failed    -- the operation did not produce a result; the slot was handed back.
#: abandoned -- the claim outlived _CLAIM_TIMEOUT without being settled either way, which in
#:              practice means the worker died mid-request. Reclaimable; slot handed back.
USAGE_STATUSES = ("claimed", "succeeded", "failed", "abandoned")

#: Which user-initiated operation this row records. Both spend the same single allowance --
#: a saved search is a search, and giving it its own budget would just be the bypass with an
#: extra step -- but they are recorded apart so the ledger says what the student actually did.
USAGE_TYPES = ("search", "saved_search_run")

#: The only non-NULL value `free_use_slot` ever holds. Named rather than written as a bare 1
#: at four call sites, because the number is meaningless on its own and the constant is not.
FREE_USE_SLOT = 1


class JobHuntUsage(Base):
    __tablename__ = "job_hunt_usages"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )

    usage_type: Mapped[str] = mapped_column(String(30), nullable=False)

    # Supplied by the client when it has one, generated server-side when it does not. Either
    # way it is only ever a *deduplication* token scoped to one profile -- it can make a
    # retry cost nothing, and it can never make a first search allowed.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # sha256 hex of the normalised search parameters. 64 chars exactly; see entitlement.py.
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="claimed", index=True)

    # 1 while this row holds the profile's single free allowance, NULL otherwise. See the
    # module docstring -- this column, plus its unique constraint, is the enforcement.
    free_use_slot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Whether the student was entitled to unlimited use at the moment they claimed. Recorded
    # rather than recomputed: subscriptions expire, and a ledger that changes its account of
    # the past when the present changes is not a ledger.
    was_premium: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.datetime.utcnow
    )
    # Set only when the operation actually succeeded. NULL on a claim still in flight and on
    # every released failure, so "when did this student spend their search" has one answer.
    consumed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # Idempotency, scoped to the owner. Two students may legitimately generate the same
        # key; neither can reach the other's row.
        UniqueConstraint("profile_id", "idempotency_key", name="uq_job_hunt_usage_idempotency"),
        # The free allowance itself. NULLs are distinct, so this permits unlimited NULL rows
        # and exactly one slot-holder per profile.
        UniqueConstraint("profile_id", "free_use_slot", name="uq_job_hunt_usage_free_slot"),
        Index("ix_job_hunt_usages_profile_status", "profile_id", "status"),
    )
