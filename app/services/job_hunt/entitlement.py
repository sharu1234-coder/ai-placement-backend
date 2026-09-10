"""Who is allowed to run a Job Hunt search, and what one run costs them.

The product rule is one sentence: a student without a subscription may run Job Hunt exactly
once, and a student with an active subscription may run it as often as they like. Almost all
of the difficulty is in the word "once".

*Once* is one operation, not one HTTP request. A single tap on "Find Jobs" turns into a
provider call, normalisation, deduplication, matching, ranking and possibly a second page --
and the student did one thing. So the allowance is spent by an *operation*, which is claimed
before the work starts and settled after it finishes, and the pages, retries and internal
round-trips inside it are free by construction rather than by remembering to exempt them.

*Once* has to survive the obvious attempts to have it twice. Reinstalling, clearing app data,
logging out and back in, and calling the API by hand all change nothing here, because nothing
here reads anything the client controls: the ledger is keyed on profile_id, which comes from
the bearer token. Changing the role, the location or the filters and asking again is not a
second free search either -- it is a second search, which is the thing being limited.

*Once* has to hold under concurrency. Two requests firing together must not both go through,
and no amount of "check, then write" achieves that. So the check *is* the write: claiming the
free search means inserting the row that holds ``free_use_slot = 1``, under a unique constraint
on (profile_id, free_use_slot). One insert lands, the other is rejected by the database, and
the outcome is the same whether the two requests hit one worker or two machines.

*Once* should not be spent on our own failures. If the provider is down, or times out, or
answers with something unusable, the claim is released and the student still has their search.
The row stays in the ledger marked failed -- the attempt happened and is auditable -- but it no
longer holds the allowance. This cannot be farmed: the student does not choose when the
provider breaks, and a released claim returns the allowance to exactly where it was.

*Once* has to tolerate a retry. Android reissues requests for reasons that have nothing to do
with intent -- a dropped connection, a recreated activity, a killed process -- and a student
should not lose their one search to a flaky train tunnel. So a request may carry an
Idempotency-Key, and a repeat of the same key inside a short window is recognised as the same
operation rather than a new one.

That key is the one place a client-supplied value enters this module, so it is worth being
exact about what it can and cannot do. It can only ever *reduce* what an operation costs, never
authorise one: there is no path where presenting a key makes a first search allowed. It is
scoped to the profile, so it cannot address another student's row. It is bound to a fingerprint
of the search parameters, so the same key with a different role or location is a conflict, not
a free ride. And it stops being a replay token after ``_REPLAY_WINDOW``, so it cannot be kept
and presented forever as a season pass. Every one of those is enforced here, server-side; the
client is trusted for nothing.
"""

import datetime
import hashlib
import json
import logging
import re
import uuid
from typing import Any, Mapping, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.job_hunt_usage import FREE_USE_SLOT, JobHuntUsage
from app.services.subscription_service import is_premium

logger = logging.getLogger(__name__)

#: How many Job Hunt operations a student gets without a subscription. One, per the product
#: rule. Named because the number appears in the API response the client renders, and a
#: literal 1 in four places is how those drift apart.
FREE_JOB_HUNT_USES = 1

#: How long a claim may stay in flight before it is assumed dead. Only reached when a worker
#: is killed between claiming and settling -- every ordinary path settles or releases in the
#: same request. Without this, one crashed process would keep a student's single search
#: permanently spoken for, with no way for them or for support to get it back.
_CLAIM_TIMEOUT = datetime.timedelta(minutes=10)

#: How long an idempotency key remains a valid replay token for an operation that already
#: succeeded. Retries caused by connection loss, activity recreation or process death all
#: happen within seconds; ten minutes is generous for those and far too short to be useful as
#: a way of re-running one search forever, which is what an unbounded window would be.
_REPLAY_WINDOW = datetime.timedelta(minutes=10)

_MAX_KEY_LENGTH = 64
# Deliberately narrow: a UUID, or anything else printable and boring. The key is stored and
# logged, so it is not a place to accept arbitrary bytes.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")

# Parameters that describe *which page of* an operation rather than *which* operation. Excluded
# from the fingerprint so that fetching page two is recognised as the same search the student
# already asked for, which is the whole point of charging per operation instead of per request.
_FINGERPRINT_EXCLUDED = frozenset({"page", "page_size"})


class JobHuntEntitlementError(Exception):
    """Base for the refusals this module can produce."""


class FreeLimitReachedError(JobHuntEntitlementError):
    """The student has no subscription and has already spent their one search."""


class IdempotencyConflictError(JobHuntEntitlementError):
    """The supplied key belongs to a different operation.

    Either it was used for a search with different parameters, or it was used for an operation
    that already completed and is no longer replayable. Both mean "this is not the request you
    are telling me it is", and neither is a reason to spend an allowance silently.
    """


class InvalidIdempotencyKeyError(JobHuntEntitlementError):
    """The supplied key is not a shape this server will store."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def normalise_key(raw: Optional[str]) -> str:
    """Validate a client-supplied key, or mint one.

    A missing key is not an error and does not weaken anything: it simply means this request
    is its own operation, so a fresh server-generated key is used and nothing can be replayed
    against it. That is the safe default -- a client that forgets to send a key gets charged
    correctly rather than accidentally deduplicated against an unrelated request.
    """
    if raw is None or not raw.strip():
        return f"srv-{uuid.uuid4().hex}"
    candidate = raw.strip()
    if len(candidate) > _MAX_KEY_LENGTH or not _KEY_PATTERN.match(candidate):
        raise InvalidIdempotencyKeyError(
            "Idempotency-Key must be 8-64 characters of letters, digits, '.', '_', ':' or '-'."
        )
    return candidate


def fingerprint(usage_type: str, params: Mapping[str, Any]) -> str:
    """A stable hash of *what was asked for*, ignoring which page of it.

    Canonicalised through JSON with sorted keys so that the same search produces the same
    fingerprint regardless of dict ordering, and so a value's type is part of the identity --
    ``salary_min: 5`` and ``salary_min: "5"`` are not the same request and must not collide.
    """
    material = {
        key: value
        for key, value in sorted(params.items())
        if key not in _FINGERPRINT_EXCLUDED and value is not None
    }
    payload = json.dumps(
        {"usage_type": usage_type, "params": material},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _find_by_key(db: Session, profile_id: int, idempotency_key: str) -> Optional[JobHuntUsage]:
    return (
        db.query(JobHuntUsage)
        # Without populate_existing an instance already in this session's identity map is
        # handed back unrefreshed, so a concurrent request's commit -- the exact thing every
        # check below is trying to see -- would be invisible. Same reasoning, and the same
        # fix, as subscription_service.verify_payment.
        .populate_existing()
        .filter(
            JobHuntUsage.profile_id == profile_id,
            JobHuntUsage.idempotency_key == idempotency_key,
        )
        .first()
    )


def _slot_holder(db: Session, profile_id: int) -> Optional[JobHuntUsage]:
    """The row currently occupying this profile's free allowance, if any."""
    return (
        db.query(JobHuntUsage)
        .populate_existing()
        .filter(
            JobHuntUsage.profile_id == profile_id,
            JobHuntUsage.free_use_slot == FREE_USE_SLOT,
        )
        .first()
    )


def _expire_stale_claims(db: Session, profile_id: int, now: datetime.datetime) -> None:
    """Hand back allowances held by claims that never finished.

    A conditional UPDATE rather than load-and-mutate, so two requests arriving at the same
    stale row cannot both believe they were the one that reclaimed it -- and it does not matter
    if they do, because the insert that follows is arbitrated by the unique constraint anyway.
    """
    result = db.execute(
        update(JobHuntUsage)
        .where(
            JobHuntUsage.profile_id == profile_id,
            JobHuntUsage.status == "claimed",
            JobHuntUsage.created_at < now - _CLAIM_TIMEOUT,
        )
        .values(status="abandoned", free_use_slot=None, updated_at=now)
    )
    if result.rowcount:
        db.commit()
        logger.warning(
            "Reclaimed %d abandoned Job Hunt claim(s) for profile %d", result.rowcount, profile_id
        )
    else:
        # Nothing was written; closing the implicit transaction keeps the session clean for the
        # insert that follows rather than leaving an empty one open across it.
        db.rollback()


def claim(
    db: Session,
    profile_id: int,
    *,
    usage_type: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> JobHuntUsage:
    """Reserve the right to run one Job Hunt operation, or refuse.

    Raises FreeLimitReachedError when the student has no subscription and no allowance left,
    and IdempotencyConflictError when the key names a different operation. Returns a row the
    caller MUST finish with settle() or release() -- see the two functions below.
    """
    now = _utcnow()
    premium = is_premium(db, profile_id)

    # Start from a fresh read snapshot. A concurrent duplicate of this very request may have
    # committed its row moments ago, and a session that opened its read transaction before
    # that commit cannot see it -- which would turn a retry into a refusal. Safe unconditionally
    # here because a claim is the first write of a request: there is nothing pending to discard.
    db.rollback()

    existing = _find_by_key(db, profile_id, idempotency_key)
    if existing is not None:
        return _resolve_existing(
            db, existing, request_fingerprint=request_fingerprint, premium=premium, now=now
        )

    if premium:
        # No slot is taken, deliberately. A subscription is unlimited use, not an early spend
        # of the free search -- so a student whose subscription lapses still has the one search
        # they never used. The row is written anyway, because the ledger's job is to record
        # what happened, not only what was rationed.
        return _insert(
            db,
            profile_id=profile_id,
            usage_type=usage_type,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            slot=None,
            premium=True,
            now=now,
        )

    _expire_stale_claims(db, profile_id, now)

    holder = _slot_holder(db, profile_id)
    if holder is not None:
        if holder.idempotency_key == idempotency_key:
            # Our own duplicate, which committed between the lookup above and this one --
            # _expire_stale_claims ends the read transaction, so the snapshot moves underneath
            # this function by design. Refusing here would report "your free search is gone" to
            # the very request that is spending it.
            return _resolve_existing(
                db, holder, request_fingerprint=request_fingerprint, premium=premium, now=now
            )
        # The cheap, common refusal. The insert below would reach the same conclusion, but
        # only by provoking an IntegrityError, and a rejected write per blocked request is a
        # poor way to answer a question a SELECT already answers.
        raise FreeLimitReachedError("Your free Job Hunt search has already been used.")

    return _insert(
        db,
        profile_id=profile_id,
        usage_type=usage_type,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        slot=FREE_USE_SLOT,
        premium=False,
        now=now,
    )


def _resolve_existing(
    db: Session,
    existing: JobHuntUsage,
    *,
    request_fingerprint: str,
    premium: bool,
    now: datetime.datetime,
) -> JobHuntUsage:
    """What a row already stored under this idempotency key means for the request in hand.

    Reached from three places -- the ordinary lookup, the slot-holder check, and the recovery
    after a lost insert race -- because a concurrent duplicate can become visible at any of
    them. Keeping the decision in one function is what stops those three paths from disagreeing
    about whether a retry costs a search.
    """
    if existing.request_fingerprint != request_fingerprint:
        # The same key carrying a different search. Standard idempotency semantics, and also
        # the specific thing that would otherwise turn the key into a season pass.
        raise IdempotencyConflictError(
            "This request id was already used for a different search."
        )
    if existing.status == "claimed":
        # A duplicate of an operation still running. Same operation, so it costs nothing more;
        # the allowance is already held by this very row.
        return existing
    if existing.status == "succeeded":
        reference = existing.consumed_at or existing.created_at
        if now - reference <= _REPLAY_WINDOW:
            # A genuine retry of something that already worked -- the client never saw the
            # response. Charging again would make a dropped connection cost a search.
            return existing
        raise IdempotencyConflictError(
            "This request id has already been used. Start a new search."
        )
    # "failed" or "abandoned": the operation never produced a result, so this is a real retry
    # and the allowance has to be taken again.
    return _reclaim(db, existing, premium=premium, now=now)


def _insert(
    db: Session,
    *,
    profile_id: int,
    usage_type: str,
    idempotency_key: str,
    request_fingerprint: str,
    slot: Optional[int],
    premium: bool,
    now: datetime.datetime,
) -> JobHuntUsage:
    usage = JobHuntUsage(
        profile_id=profile_id,
        usage_type=usage_type,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        status="claimed",
        free_use_slot=slot,
        was_premium=premium,
        created_at=now,
        updated_at=now,
    )
    try:
        # A savepoint, not a bare try: an IntegrityError on a plain flush poisons the whole
        # transaction, and db.rollback() here would also discard anything the request had
        # already done. This scopes the undo to exactly this failed insert.
        with db.begin_nested():
            db.add(usage)
            db.flush()
    except IntegrityError:
        # Two constraints can reject this, and they mean opposite things -- so the next step
        # is to find out which, by looking for a row under this key.
        #
        # The rollback is not optional, and this is the part that a sequential test cannot
        # show. The savepoint undid the failed INSERT, but this session's read snapshot was
        # opened before the concurrent commit that caused the rejection, so re-reading inside
        # the same transaction still reports "no such row" -- and the request would then be
        # told its free search was gone when in fact its own duplicate had just taken it.
        # Ending the transaction is what makes the following read see the committed truth.
        # Safe here specifically because a claim is the first write of a request: there is
        # nothing else in this transaction for the rollback to discard.
        db.rollback()
        raced = _find_by_key(db, profile_id, idempotency_key)
        if raced is not None:
            # Same key, inserted by a concurrent duplicate of this very request -- resolved by
            # the same rules as a sequential retry, including the fingerprint check, so that
            # "send the second search fast enough" is not a way around it.
            return _resolve_existing(
                db, raced, request_fingerprint=request_fingerprint, premium=premium, now=now
            )
        # The free-slot constraint, then: another request won the race for the one search.
        raise FreeLimitReachedError("Your free Job Hunt search has already been used.")
    db.commit()
    db.refresh(usage)
    return usage


def _reclaim(
    db: Session, usage: JobHuntUsage, *, premium: bool, now: datetime.datetime
) -> JobHuntUsage:
    """Re-take the allowance for a retry of an operation that previously failed."""
    try:
        with db.begin_nested():
            usage.status = "claimed"
            usage.free_use_slot = None if premium else FREE_USE_SLOT
            usage.was_premium = premium
            usage.updated_at = now
            db.flush()
    except IntegrityError:
        # Between the original failure and this retry, some other request took the free
        # search. The student is out of allowance; this is no different from any other
        # refusal. The rollback discards the in-memory mutation along with the failed UPDATE,
        # so the row is left exactly as it was rather than as a half-applied retry.
        db.rollback()
        raise FreeLimitReachedError("Your free Job Hunt search has already been used.")
    db.commit()
    db.refresh(usage)
    return usage


def settle(db: Session, usage: JobHuntUsage) -> None:
    """Record that the operation produced a real result. The allowance is now spent.

    Conditional on the row still being "claimed", which makes it idempotent: settling a row a
    concurrent duplicate already settled writes nothing and is not an error.
    """
    now = _utcnow()
    db.execute(
        update(JobHuntUsage)
        .where(JobHuntUsage.id == usage.id, JobHuntUsage.status == "claimed")
        .values(status="succeeded", consumed_at=now, updated_at=now)
    )
    db.commit()


def release(db: Session, usage: JobHuntUsage) -> None:
    """Hand the allowance back: the operation did not produce a result.

    Also conditional on "claimed", and that condition carries real weight. If this row already
    succeeded -- a replayed key whose second run happened to fail -- the earlier success stays
    consumed. Otherwise a student could spend their search, get results, then trigger one
    failure and have the search back.
    """
    now = _utcnow()
    # The failure may have left uncommitted work in the session (a partially built search).
    # Dropping it first means this write is the only thing that lands, and that no later
    # rollback can take the release with it.
    db.rollback()
    db.execute(
        update(JobHuntUsage)
        .where(JobHuntUsage.id == usage.id, JobHuntUsage.status == "claimed")
        .values(status="failed", free_use_slot=None, updated_at=now)
    )
    db.commit()


def status_for(db: Session, profile_id: int) -> dict:
    """What the client needs to render the right screen, decided entirely server-side.

    This is a UX aid and nothing more. Every field here is re-derived on the request that
    actually runs a search, so a client that lies to itself about this response -- or never
    calls it at all -- changes nothing about what it is allowed to do.
    """
    premium = is_premium(db, profile_id)
    holder = _slot_holder(db, profile_id)
    used = 0 if holder is None else FREE_JOB_HUNT_USES
    return {
        "tier": "premium" if premium else "free",
        "unlimited": premium,
        "free_uses_total": FREE_JOB_HUNT_USES,
        "free_uses_remaining": FREE_JOB_HUNT_USES - used,
        "has_used_free_search": holder is not None,
        # False for a premium student even after they have spent the free search, because the
        # question this answers is "would the next search be refused", not "was one used".
        "requires_premium": (not premium) and holder is not None,
        "last_free_use_at": holder.consumed_at if holder is not None else None,
    }
