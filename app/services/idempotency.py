"""Make a replayed mutation safe, so the network is free to retry.

The contract, in one line: one logical user operation may arrive any number of times, and
produces at most one durable mutation.

A route uses it in three steps::

    outcome = idempotency.claim(db, profile.id, operation="assessment.start",
                                idempotency_key=key, params={"blueprint_id": 7})
    if outcome.is_replay:
        return _read_back(db, outcome.result_id)      # the first attempt's own result
    try:
        created = do_the_real_work(...)
    except Exception:
        idempotency.release(db, outcome.claim_id)     # a real failure: a retry may proceed
        raise
    idempotency.settle(db, outcome.claim_id, result_kind="assessment", result_id=created.id)
    return created

**claim() commits before returning, and that is deliberate.** Two things depend on it. A
concurrent duplicate has to be able to *see* the claim, which an uncommitted row does not
provide. And the operations being protected here include ones that then spend minutes in a
model: Phase 19 (F12-07) requires no transaction to be open across a provider call, so the
claim must be settled business before that call starts, not an open transaction carried into it.

**The savepoint handling below is lifted from job_hunt/entitlement.py**, which already solved
this for the Job Hunt allowance. It is reproduced rather than imported because that module's
claim is entangled with the free-use slot and premium state; the part worth sharing is the
reasoning, which is repeated in the comments at each step. The one piece that a sequential test
cannot show, and that is easy to delete by accident, is the `db.rollback()` after an
IntegrityError -- see `_insert`.
"""

import datetime
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.operation_claim import OperationClaim

logger = logging.getLogger(__name__)

#: How long a claim may stay in flight before it is assumed dead and may be re-taken. Only
#: reached when a worker is killed between claiming and settling -- every ordinary path settles
#: or releases in the same request. Generous enough to cover the slowest protected operation: a
#: placement-path generation was measured at ~163s against a local model, and a resume parse plus
#: analysis is the same order, so a few minutes of headroom is deliberate rather than arbitrary.
CLAIM_TIMEOUT = datetime.timedelta(minutes=15)

#: How long a succeeded key stays a valid replay token. Transport retries, activity recreation
#: and process death all happen within seconds of the original; a day is far more than those
#: need and still bounded, so a key cannot be used as a permanent handle on an old result.
REPLAY_WINDOW = datetime.timedelta(hours=24)

#: How long a settled row is kept at all. Past this it carries no meaning -- the replay window
#: has long closed -- and it is only taking space, so claims sweep their own profile's old rows.
RETENTION = datetime.timedelta(days=7)

_MAX_KEY_LENGTH = 64
#: Same alphabet the Job Hunt key already accepts, so a client can mint one key format for every
#: protected endpoint. A UUID hex with a short prefix fits comfortably.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


class IdempotencyError(Exception):
    """Base class, so a route can catch the family."""


class InvalidIdempotencyKeyError(IdempotencyError):
    """The client sent something that is not a usable key."""


class IdempotencyConflictError(IdempotencyError):
    """This key already names a different request, or an expired one."""


class OperationInProgressError(IdempotencyError):
    """The same operation is still running. Deliberately not a second attempt at it."""


def _utcnow() -> datetime.datetime:
    # Value-identical to the deprecated datetime.utcnow(); every DateTime column in this schema
    # is naive UTC, and mixing an aware value in would compare wrongly rather than loudly.
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def normalise_key(raw: Optional[str]) -> str:
    """Validate a client-supplied key, or mint one.

    A missing key is not an error and weakens nothing: it means this request is its own
    operation, so a fresh server-side key is used and nothing can be replayed against it. A
    client that forgets to send one therefore behaves exactly as it did before this mechanism
    existed, rather than being silently deduplicated against an unrelated request.
    """
    if raw is None or not raw.strip():
        return f"srv-{uuid.uuid4().hex}"
    candidate = raw.strip()
    if len(candidate) > _MAX_KEY_LENGTH or not _KEY_PATTERN.match(candidate):
        raise InvalidIdempotencyKeyError(
            "Idempotency-Key must be 8-64 characters of letters, digits, '.', '_', ':' or '-'."
        )
    return candidate


def fingerprint(operation: str, params: Mapping[str, Any]) -> str:
    """A stable hash of what this operation was asked to do.

    Canonicalised through JSON with sorted keys, so the same request hashes the same regardless
    of dict ordering, and a value's type is part of the identity -- ``blueprint_id: 5`` and
    ``blueprint_id: "5"`` are not the same request and must not collide.

    `operation` is part of the material, which is what stops one key from being reused across
    two different endpoints: the second arrives as a conflict rather than being handed the
    first one's unrelated result.

    Callers pass the mutation-defining inputs only. Nothing here should ever receive an
    Authorization header, a token, a payment secret or a request body -- for a file upload the
    caller passes a digest of the bytes, not the bytes.
    """
    material = {
        key: value for key, value in sorted(params.items()) if value is not None
    }
    payload = json.dumps(
        {"operation": operation, "params": material},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def body_digest(data: bytes) -> str:
    """A bounded fingerprint input for an upload: the file's own hash, never the file.

    Separate from [fingerprint] so the intent is visible at the call site, and so there is one
    obvious thing to pass for a multipart route rather than a temptation to pass `data` itself.
    """
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Outcome:
    """What `claim` decided.

    `is_replay` means the work is already done and `result_kind`/`result_id` name it; the route
    should read that row back rather than doing anything. Otherwise this is a fresh claim the
    route owns and must settle or release, identified by `claim_id`.

    A plain integer id rather than the ORM object, deliberately. The protected routes commit and
    roll back in the middle of their own work -- the resume upload commits a Resume, then may
    roll back after a failed parse -- and an instance carried across that becomes detached, so
    `settle` would raise DetachedInstanceError while trying to read its own primary key. An int
    cannot detach. (Found by running this against the existing resume and subscription suites.)
    """

    claim_id: Optional[int]
    is_replay: bool
    status: Optional[str] = None
    result_kind: Optional[str] = None
    result_id: Optional[int] = None


def _find_by_key(db: Session, profile_id: int, idempotency_key: str) -> Optional[OperationClaim]:
    # profile_id is in the filter, not just the key. This is the whole of the ownership
    # guarantee: a key belonging to another student simply does not match, so there is no path
    # by which profile A replays profile B's operation or sees its result.
    return (
        db.query(OperationClaim)
        .filter(
            OperationClaim.profile_id == profile_id,
            OperationClaim.idempotency_key == idempotency_key,
        )
        .one_or_none()
    )


def _is_stale(record: OperationClaim, now: datetime.datetime) -> bool:
    """A claim whose worker died between claiming and settling.

    Decided by reading the row rather than by a blanket UPDATE over the profile's claims. The
    first version did the UPDATE up front, which meant every request -- including a pure replay
    that needs nothing but a SELECT -- opened a write transaction before it had even looked. On
    SQLite, five concurrent duplicates then contended for the single write lock and two of them
    came back "database is locked" instead of being told the operation was in progress. The
    invariant still held, but a retry storm is exactly when this path must not be fighting
    itself, so the read path is now read-only.
    """
    return record.status == "claimed" and record.created_at < now - CLAIM_TIMEOUT


def _sweep_old_rows(db: Session, profile_id: int, now: datetime.datetime) -> None:
    """Retention, done where it costs nothing: this profile's own long-settled rows.

    Opportunistic rather than scheduled, the way password_reset_service already prunes its
    tokens. It needs no new process, no cron entry and no infrastructure, which is the point --
    a sweep that requires a service to be running is a sweep that stops happening. Rows are only
    ever removed well past REPLAY_WINDOW, so nothing a client could still legitimately replay
    against is in scope.
    """
    db.execute(
        delete(OperationClaim).where(
            OperationClaim.profile_id == profile_id,
            OperationClaim.status.in_(("succeeded", "failed", "abandoned")),
            OperationClaim.created_at < now - RETENTION,
        )
    )


def claim(
    db: Session,
    profile_id: int,
    *,
    operation: str,
    idempotency_key: Optional[str],
    params: Mapping[str, Any],
) -> Outcome:
    """Take this operation, or report that it is already taken.

    Commits before returning -- see the module docstring for why that matters to both
    concurrency and Phase 19.
    """
    key = normalise_key(idempotency_key)
    request_fingerprint = fingerprint(operation, params)
    now = _utcnow()

    # Look before writing. A replay -- the case this whole mechanism exists to serve, and the
    # one that arrives in bursts -- resolves from here without taking a write lock at all.
    existing = _find_by_key(db, profile_id, key)
    if existing is not None:
        return _resolve_existing(db, existing, request_fingerprint=request_fingerprint, now=now)

    outcome = _insert(
        db,
        profile_id=profile_id,
        operation=operation,
        idempotency_key=key,
        request_fingerprint=request_fingerprint,
        now=now,
    )
    # Retention runs only on the path that was going to write anyway, and only for this
    # profile. A replay never pays for it, and it needs no scheduler.
    if not outcome.is_replay:
        _sweep_old_rows(db, profile_id, now)
        db.commit()
    return outcome


def _resolve_existing(
    db: Session,
    existing: OperationClaim,
    *,
    request_fingerprint: str,
    now: datetime.datetime,
) -> Outcome:
    """What a row already stored under this key means for the request in hand.

    Reached from two places -- the ordinary lookup and the recovery after a lost insert race --
    because a concurrent duplicate can become visible at either. Keeping the decision in one
    function is what stops those paths from disagreeing about whether a replay repeats work.
    """
    if existing.request_fingerprint != request_fingerprint:
        # Same key, different request. Standard idempotency semantics, and also the specific
        # thing that would otherwise let a key be reused to re-enter an unrelated operation.
        raise IdempotencyConflictError(
            "This request id was already used for a different request."
        )

    if existing.status == "claimed":
        if _is_stale(existing, now):
            # The worker that held this died between claiming and settling. Without this a
            # student would be locked out of their own operation until the row aged out.
            return _reclaim(db, existing, now=now)
        # A duplicate of something still running. Refusing is the point: the first attempt owns
        # the mutation and, for the AI-backed routes, is already paying for the model call.
        raise OperationInProgressError(
            "That request is still being processed. Please wait for it to finish."
        )

    if existing.status == "succeeded":
        reference = existing.completed_at or existing.created_at
        if now - reference <= REPLAY_WINDOW:
            # A genuine replay of something that already worked -- the client never saw the
            # response. This is the case the whole phase exists for.
            return Outcome(
                claim_id=existing.id,
                is_replay=True,
                status=existing.status,
                result_kind=existing.result_kind,
                result_id=existing.result_id,
            )
        raise IdempotencyConflictError(
            "This request id has already been used. Start a new request."
        )

    # "failed" or "abandoned": nothing durable came of it, so this is a real retry and is
    # allowed to proceed. Re-taking the same row keeps one row per logical operation rather
    # than accumulating one per attempt.
    return _reclaim(db, existing, now=now)


def _insert(
    db: Session,
    *,
    profile_id: int,
    operation: str,
    idempotency_key: str,
    request_fingerprint: str,
    now: datetime.datetime,
) -> Outcome:
    record = OperationClaim(
        profile_id=profile_id,
        operation=operation,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        status="claimed",
        created_at=now,
        updated_at=now,
    )
    try:
        # A savepoint, not a bare try: an IntegrityError on a plain flush poisons the whole
        # transaction, and a db.rollback() here would also discard anything the request had
        # already done. This scopes the undo to exactly this failed insert.
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError:
        # Only one constraint can reject this -- (profile_id, idempotency_key) -- so a
        # concurrent duplicate of this very request got here first.
        #
        # The rollback is not optional, and this is the part a sequential test cannot show. The
        # savepoint undid the failed INSERT, but this session's read snapshot was opened before
        # the concurrent commit that caused the rejection, so re-reading inside the same
        # transaction still reports "no such row" -- and the request would then try to insert
        # again, and fail again, forever. Ending the transaction is what makes the read below
        # see the committed truth. Safe here specifically because a claim is the first write of
        # a request: there is nothing else in this transaction for the rollback to discard.
        db.rollback()
        raced = _find_by_key(db, profile_id, idempotency_key)
        if raced is None:
            # Nothing under the key after all. Not reachable from the single constraint on this
            # table, so it means the schema grew another one; say so rather than guess.
            raise
        return _resolve_existing(
            db, raced, request_fingerprint=request_fingerprint, now=now
        )
    db.commit()
    db.refresh(record)
    return Outcome(claim_id=record.id, is_replay=False, status=record.status)


def _reclaim(db: Session, record: OperationClaim, *, now: datetime.datetime) -> Outcome:
    """Re-take a row whose operation previously failed or was abandoned."""
    db.execute(
        update(OperationClaim)
        .where(OperationClaim.id == record.id)
        .values(status="claimed", result_kind=None, result_id=None,
                completed_at=None, updated_at=now)
    )
    db.commit()
    db.refresh(record)
    return Outcome(claim_id=record.id, is_replay=False, status=record.status)


def run_once(
    db: Session,
    profile_id: int,
    *,
    operation: str,
    idempotency_key: Optional[str],
    params: Mapping[str, Any],
    perform: "Callable[[], tuple[Any, Optional[int]]]",
    read_back: "Callable[[int], Any]",
    result_kind: str,
) -> Any:
    """claim -> perform -> settle, with release on failure. The shape every route uses.

    `perform` returns ``(response, result_id)``: the thing to send back, and the row id to
    record so a replay can find it again. `read_back` turns a recorded id into that same
    response, by re-reading the live row -- which is why nothing is cached here and why a
    replay cannot serve a stale copy.

    Kept as one function rather than a context manager because a replay has to skip the body
    entirely, and a `with` block cannot do that without raising.
    """
    outcome = claim(
        db, profile_id, operation=operation, idempotency_key=idempotency_key, params=params
    )
    if outcome.is_replay:
        if outcome.result_id is None:
            # Settled with no row to point at -- an operation whose durable effect was not a
            # single row. Nothing to re-read, so treat it as done and let the caller decide.
            return None
        existing = read_back(outcome.result_id)
        if existing is not None:
            return existing
        # The row was deleted between the original request and this replay. Re-running would
        # be a second mutation under a key that already means something, and pretending it is
        # still there would be a lie, so this is a conflict the client must resolve with a new
        # request rather than a retry of this one.
        raise IdempotencyConflictError(
            "The result of that request no longer exists. Please start a new request."
        )

    try:
        response, result_id = perform()
    except Exception:
        # Nothing durable came of it, so an explicit retry under the same key must be allowed.
        release(db, outcome.claim_id)
        raise
    settle(db, outcome.claim_id, result_kind=result_kind, result_id=result_id)
    return response


async def run_once_async(
    db: Session,
    profile_id: int,
    *,
    operation: str,
    idempotency_key: Optional[str],
    params: Mapping[str, Any],
    perform: "Callable[[], Any]",
    read_back: "Callable[[int], Any]",
    result_kind: str,
) -> Any:
    """[run_once] for a route that has to await its work.

    Only the resume upload needs this today: that route is `async def` because it awaits
    `file.read()`, and it hands the blocking parse to a threadpool. The claim/settle decisions
    are identical -- duplicated rather than abstracted over sync/async because the alternative
    is a wrapper that hides which of the two is running.
    """
    outcome = claim(
        db, profile_id, operation=operation, idempotency_key=idempotency_key, params=params
    )
    if outcome.is_replay:
        if outcome.result_id is None:
            return None
        existing = read_back(outcome.result_id)
        if existing is not None:
            return existing
        raise IdempotencyConflictError(
            "The result of that request no longer exists. Please start a new request."
        )

    try:
        response, result_id = await perform()
    except Exception:
        release(db, outcome.claim_id)
        raise
    settle(db, outcome.claim_id, result_kind=result_kind, result_id=result_id)
    return response


def settle(
    db: Session, claim_id: int, *, result_kind: str, result_id: Optional[int]
) -> None:
    """Record that the operation produced this durable result.

    Conditional on the row still being "claimed", which makes it idempotent: settling a row a
    concurrent duplicate already settled writes nothing and is not an error.
    """
    now = _utcnow()
    db.execute(
        update(OperationClaim)
        .where(OperationClaim.id == claim_id, OperationClaim.status == "claimed")
        .values(
            status="succeeded",
            result_kind=result_kind,
            result_id=result_id,
            completed_at=now,
            updated_at=now,
        )
    )
    db.commit()


def release(db: Session, claim_id: int) -> None:
    """Record that the operation produced nothing, so an explicit retry may proceed.

    Also conditional on "claimed", and that condition carries weight: if this row already
    succeeded -- a replayed key whose second run happened to fail -- the earlier success stays
    recorded rather than being overwritten by the later failure.

    The rollback first is what makes this survivable. The failure may have left uncommitted work
    in the session, and dropping it means this write is the only thing that lands and that no
    later rollback can take the release with it.
    """
    now = _utcnow()
    db.rollback()
    db.execute(
        update(OperationClaim)
        .where(OperationClaim.id == claim_id, OperationClaim.status == "claimed")
        .values(status="failed", updated_at=now)
    )
    db.commit()


# ---------------------------------------------------------------------------
# Business-level concurrency, layered on the same table (Phase 25)
# ---------------------------------------------------------------------------
#
# Everything above answers one question: "is this HTTP request a replay of one I already
# handled?" The key comes from the client, and two taps the client considers different requests
# are different operations -- which is correct, and is exactly why it cannot answer the second
# question:
#
#     "is some *other* request already generating the one resource this one is about to create?"
#
# Request A arrives with key X, request B with key Y. Different keys, so neither can see the
# other, and both are entitled to proceed. If they are both "build the preparation plan for job
# description 100", the student pays for two model calls and the table ends up with two plans,
# one of which every read path silently discards.
#
# The fix needs no second mechanism. A business operation is just an operation whose key the
# *server* derives from the resource instead of taking from the client, so two requests about
# the same resource necessarily collide on `UNIQUE (profile_id, idempotency_key)` -- the same
# constraint, doing the same job, for an identity the client does not get a vote on.
#
# Two differences from the request layer, both deliberate:
#
#   * A business claim has no replay window. REPLAY_WINDOW exists because a client's retry of a
#     day-old request is a mistake worth refusing. "This job description already has a plan" is
#     not a retry and does not expire, so the durable resource is what gets consulted, and the
#     claim is consulted only while the work is in flight.
#
#   * A duplicate does not get an error if it can be given the answer instead. The request layer
#     refuses a replay of something still running, because the client already has that request's
#     outcome coming. Here the second request is a *different* request that wants the same
#     result, so it waits for the first and is answered from it.
#
# What this layer is not allowed to do is weaken the layer above. The request claim is still
# taken first and still settles or releases on its own terms, so same key with a different
# fingerprint is still a conflict and a transport replay is still answered from the original
# claim -- this only decides who is allowed to do the work behind them.

#: How long a duplicate will wait for the request that owns the generation.
#:
#: Picked from the timings that actually bound it rather than a round number. The production
#: provider is capped at `anthropic_timeout_seconds` (120s) and a local Ollama at 240s, so a
#: generation can legitimately outlast any wait worth holding a worker for; the Android client's
#: read timeout is 300s, so the waiter has to return well inside that or the student sees a
#: transport failure instead of an answer. 60s sits above every plan generation observed in this
#: app while leaving 240s of client margin, and it bounds how long one duplicate can occupy a
#: threadpool worker at one minute.
#:
#: Exceeding it does not break the invariant. The waiter gets the same in-progress response a
#: duplicate key already gets today, and because every client reads before it writes here, the
#: student's next attempt finds the finished plan with a GET and no model call at all.
BUSINESS_WAIT = datetime.timedelta(seconds=60)

#: Poll backoff while waiting, in seconds. Starts fast so a quick generation is answered quickly,
#: then backs off so a slow one is not a tight loop -- about 45 queries across the full 60s, each
#: one an indexed single-row SELECT with no transaction held open across the sleep.
BUSINESS_POLL_FIRST = 0.2
BUSINESS_POLL_MAX = 2.0
BUSINESS_POLL_GROWTH = 1.5

#: Separates the parts of a derived key, and is deliberately a character `_KEY_PATTERN` rejects.
#: That is what stops a client from sending one: a request carrying `biz@jd.preparation_plan@100`
#: is refused as a malformed Idempotency-Key before it could be mistaken for the server's own
#: business claim, so there is no path by which a client hijacks one or is hijacked by one.
_BUSINESS_MARK = "@"
_BUSINESS_PREFIX = f"biz{_BUSINESS_MARK}"

#: What a *settled* business claim is worth to a request that arrives later. The two operations
#: that use this layer need opposite answers, and the difference is a product fact rather than a
#: technical one, so it is named at the call site instead of being guessed here.
#:
#: REPLAY -- the operation produces one resource that then exists for good. A preparation plan
#:   belongs to a job description forever: no regenerate route, no delete route, and every caller
#:   means "make sure this exists". So a settled claim answers everybody, and the resource -- not
#:   the claim -- is what gets consulted, because claims are swept and the resource is not.
#:
#: REGENERATE -- the operation is itself repeatable. POST /placement-path/generate is documented
#:   as "creates the student's first placement path, or regenerates it (superseding the previous
#:   one, never deleting it) -- the same operation either way", and the Android client says the
#:   same thing from its own side: it clears its key on success precisely so that "a later
#:   deliberate regeneration is a new one and must not be answered with the path this call
#:   produced". Treating a settled claim as a replay here would not be caution. It would delete
#:   the feature.
#:
#:   So a settled claim answers only a request that watched this generation run -- one that found
#:   the claim in flight and waited for it. Such a request is a concurrent duplicate by
#:   construction and is entitled to the winner's result. A request that finds the claim already
#:   settled never overlapped anything, so it is a new deliberate operation and takes it over.
#:
#: Deciding that by "did I wait?" rather than by a time window is what makes it exact. A window
#: would be a guess about how quickly a student can mean two different things, and it would be
#: wrong in both directions: too short and a duplicate buys a second 163-second generation, too
#: long and a deliberate regeneration silently returns the old path and looks broken.
REPLAY = "replay"
REGENERATE = "regenerate"


def business_key(operation: str, identity: Mapping[str, Any]) -> str:
    """The claim key for a business resource, derived rather than supplied.

    Left readable while it fits -- `biz@jd.preparation_plan@100` is worth being able to read in a
    row -- and hashed past that, because the column holds 64 characters and an identity with
    several parts would otherwise be truncated into a collision with its neighbours. Both forms
    keep the prefix, so they are still recognisable as the same kind of thing and neither is
    something a client could send.
    """
    parts = _BUSINESS_MARK.join(f"{name}={identity[name]}" for name in sorted(identity))
    plain = f"{_BUSINESS_PREFIX}{operation}{_BUSINESS_MARK}{parts}"
    if len(plain) <= _MAX_KEY_LENGTH:
        return plain
    digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return f"{_BUSINESS_PREFIX}{digest}"[:_MAX_KEY_LENGTH]


def run_business_once(
    db: Session,
    profile_id: int,
    *,
    operation: str,
    identity: Mapping[str, Any],
    perform: "Callable[[], tuple[Any, Optional[int]]]",
    read_back: "Callable[[Optional[int]], Any]",
    result_kind: str,
    on_settled: str = REPLAY,
) -> Any:
    """Run `perform` for this business resource, or hand back what another request produced.

    `identity` is the resource, not the request: the values that decide *which* resource this is,
    and nothing else. Two requests with different bodies but the same identity are the same
    business operation, and only one of them runs.

    `read_back` receives the settled claim's result id, as the request layer's does, and may
    ignore it. Whether it should depends on the operation, and both answers are right for their
    own: a resource that exists for good is better looked up by its identity, because the claim
    that recorded it is swept long before the resource goes away, while a repeatable operation's
    waiter wants precisely the row that the generation it watched produced and nothing else.

    `on_settled` says what a settled claim is worth to a later request -- see REPLAY and
    REGENERATE above. It defaults to REPLAY, which is the safer default to get wrong: that way
    costs a student a regeneration they asked for, which is visible and complainable, where the
    other way round quietly spends money.

    Commits the claim before `perform` runs and holds no transaction while it waits, so an AI
    call inside `perform` still happens outside any transaction (Phase 19).
    """
    key = business_key(operation, identity)
    request_fingerprint = fingerprint(operation, dict(identity))
    deadline = time.monotonic() + BUSINESS_WAIT.total_seconds()
    delay = BUSINESS_POLL_FIRST
    #: Whether this request ever found the claim in flight. For a REGENERATE operation this is
    #: the entire distinction between "I am a concurrent duplicate, give me the winner's result"
    #: and "I am a new deliberate request, let me generate" -- and it is a fact about this
    #: request, which is why no amount of looking at the row can substitute for it.
    waited = False

    while True:
        now = _utcnow()
        existing = _find_by_key(db, profile_id, key)

        if existing is None:
            try:
                outcome = _insert(
                    db,
                    profile_id=profile_id,
                    operation=operation,
                    idempotency_key=key,
                    request_fingerprint=request_fingerprint,
                    now=now,
                )
            except OperationInProgressError:
                # Lost the insert race: a duplicate claimed this resource between the read above
                # and this INSERT. `_insert` resolves that through `_resolve_existing`, which
                # applies the *request* layer's policy and refuses -- correct there, because a
                # client replaying its own key already has that request's outcome coming, and
                # wrong here, because this is a different request that wants the same result.
                #
                # So the refusal is converted back into the decision this layer was going to make
                # anyway. Going round the loop re-reads the row the winner committed and falls
                # into the waiting branch below. Without this, one of five concurrent requests was
                # answered 409 while the other four were answered with the plan -- the invariant
                # held, and the student whose request happened to lose a microsecond race still
                # saw an error. Measured, not guessed: it was the first failure this file found.
                continue
        elif existing.status == "succeeded" and (on_settled == REPLAY or waited):
            # No replay window here, on purpose -- see the section note above. The claim says a
            # generation finished; whether the resource survived is the resource's own business.
            done = read_back(existing.result_id)
            if done is not None:
                return done
            # It was generated and then deleted -- the student deleted the job description, or an
            # account sweep ran. There is nothing to hand back and nothing left to protect, so
            # this is a genuine first generation again rather than a conflict.
            logger.info(
                "business claim for operation=%s has no surviving result; regenerating", operation
            )
            outcome = _reclaim(db, existing, now=now)
        elif existing.status == "claimed" and not _is_stale(existing, now):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Still running, and we are out of patience. Deliberately the same error a
                # duplicate key already raises, so no status a client has never been taught to
                # read reaches one.
                raise OperationInProgressError(
                    "That request is still being processed. Please wait for it to finish."
                )
            # Ends the transaction before sleeping, which returns the pooled connection and means
            # the next read opens a fresh snapshot that can see the owner's commit. Polling inside
            # one long-lived transaction would re-read the same stale snapshot until the deadline.
            db.rollback()
            time.sleep(min(delay, remaining))
            delay = min(delay * BUSINESS_POLL_GROWTH, BUSINESS_POLL_MAX)
            waited = True
            continue
        else:
            # "failed", "abandoned", a claim whose worker died mid-generation, or -- for a
            # REGENERATE operation -- one that had already succeeded before this request arrived.
            # Nothing is owed to this request in any of those cases, so it takes the claim over.
            outcome = _reclaim(db, existing, now=now)

        if outcome.is_replay:
            # Lost the insert race to a concurrent duplicate that has since settled, which means
            # this request did overlap it even though it never saw the row in flight. Read what it
            # produced; if it produced nothing, decide again from the row it left.
            done = read_back(outcome.result_id)
            if done is not None:
                return done
            continue

        try:
            response, result_id = perform()
        except Exception:
            release(db, outcome.claim_id)
            raise
        settle(db, outcome.claim_id, result_kind=result_kind, result_id=result_id)
        return response
