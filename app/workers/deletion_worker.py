"""Phase 2 of account deletion: erase the objects, then the last user row.

Access was already revoked in Phase 1, so nothing here is urgent for the user's safety -- what
it is responsible for is finishing. The design constraints, all from
docs/proposal-account-deletion-outbox.md:

* **Every job is idempotent.** Deleting an object that is already gone is success, because the
  obligation is "the object is not there", not "this process removed it".
* **"Not there" means no version of it is stored.** On a versioned bucket a plain delete only
  inserts a delete marker: the object stops being listed, the API answers 404, and every byte
  is still there. So the job is finished through `StorageProvider.erase`, which deletes each
  version by id and then re-enumerates to confirm nothing is left, and which raises rather than
  returning when it cannot confirm that. A job reaches `done` on nothing weaker.
* **Concurrent workers never both do the same job.** The claim is a single statement using
  `FOR UPDATE SKIP LOCKED`, so a second worker steps over a locked row instead of waiting for
  it or taking it twice.
* **A crash costs a lease, not a job.** The claim stamps `locked_until`; a worker that dies
  mid-job leaves the lease to expire and the job becomes claimable again. Nothing has to
  notice the crash.
* **No retry ceiling closes the obligation.** `needs_attention` caps the backoff and makes the
  job visible to operators; it never stops the job being retried. A terminal "failed" status
  would quietly convert an unfinished erasure into an abandoned one.
* **Nothing personal reaches a log.** Storage keys, bucket names and exception messages are
  withheld; the request uuid, the job id and the exception *class* are what gets recorded.

The internal targets below are provisional engineering numbers **pending privacy and legal
review** (section 11.2). Nothing user-facing may quote them.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.deletion_job import (
    STATUS_DONE,
    STATUS_NEEDS_ATTENTION,
    STATUS_PENDING,
    DeletionJob,
)
from app.models.deletion_request import UNRESOLVED_STATUSES, DeletionRequest
from app.models.user import User
from app.storage.erasure import ERASURE_NOT_IMPLEMENTED, IMMEDIATE_ATTENTION_CODES
from app.storage.identity import StorageIdentity
from app.storage.provider import StorageError, StorageProvider

logger = logging.getLogger(__name__)

# --- provisional internal targets, pending privacy/legal review (section 11.2) -------------
# A job unclaimed after this long means the worker is not running, which is a monitoring
# signal rather than something to retry harder.
PICKUP_TARGET = datetime.timedelta(minutes=5)
# Page-worthy: long enough to rule out a slow queue, short enough to catch a misconfiguration
# (AccessDenied, a missing bucket) well before any retry interval has grown.
ALERT_AFTER = datetime.timedelta(hours=1)
# A person looks at it. At this point it is a fault, not a queue being slow.
ESCALATE_AFTER = datetime.timedelta(hours=24)

# The lease is the pickup target: a crashed worker's job is reclaimable inside the window the
# queue is meant to drain in anyway.
LEASE = PICKUP_TARGET
# Backoff doubles from here and stops growing at the cap, which is also where the job becomes
# `needs_attention`. Capping matters: unbounded backoff is how an obligation goes quiet.
BACKOFF_BASE = datetime.timedelta(seconds=30)
BACKOFF_CAP = datetime.timedelta(minutes=30)
# Attempts after which the job is *also* flagged for a human. Not a ceiling on retries.
ATTENTION_AFTER_ATTEMPTS = 5

#: The failure classification now lives in `app.storage.erasure`, beside the codes it
#: classifies, and is imported above. It moved there when individual resume deletion began
#: classifying the same failures: the account-level and the resume-level answer have to come
#: from one copy, or the narrower path can quietly become the more permissive one.
#:
#: What it means here: everything in `IMMEDIATE_ATTENTION_CODES` goes straight to
#: `needs_attention` instead of waiting for ATTENTION_AFTER_ATTEMPTS, exactly as a provider
#: mismatch does.

#: Why a job may not proceed to a destructive call. Each is an explicit non-success state, never
#: an idempotent success, and each stays claimable so it resumes once an operator fixes the
#: cause.
IDENTITY_UNKNOWN = "StorageIdentityUnknown"
IDENTITY_INCOMPLETE = "StorageIdentityIncomplete"
IDENTITY_UNDETERMINABLE = "StorageIdentityUndeterminable"
IDENTITY_MISMATCH = "StorageIdentityMismatch"
#: The job recorded an owner guard and this worker is configured with a different one.
IDENTITY_OWNER_MISMATCH = "StorageOwnerMismatch"
#: The job recorded an owner guard and this worker has none, so it cannot assert it on the
#: wire. Distinct from a mismatch because the fix is different: one is a wrong value, the
#: other is a missing setting.
IDENTITY_OWNER_UNCONFIRMED = "StorageOwnerUnconfirmed"

#: The identity failures, for the operator report and for tests that assert none of them can
#: reach storage.
IDENTITY_FAILURES = frozenset(
    {
        IDENTITY_UNKNOWN,
        IDENTITY_INCOMPLETE,
        IDENTITY_UNDETERMINABLE,
        IDENTITY_MISMATCH,
        IDENTITY_OWNER_MISMATCH,
        IDENTITY_OWNER_UNCONFIRMED,
    }
)


def recorded_identity(job: DeletionJob) -> Optional[StorageIdentity]:
    """The destination this job was enqueued against, or None if it was never captured.

    `storage_config_revision` is the marker rather than the bucket: a job whose provider is
    `local` has no bucket either way, so without the revision there is no way to tell a job
    created before identity capture from one created after it.
    """
    if not job.storage_config_revision:
        return None
    return StorageIdentity(
        provider=job.storage_provider,
        bucket=job.storage_bucket,
        endpoint=job.storage_endpoint,
        region=job.storage_region,
        expected_owner=job.storage_expected_owner,
    )


def identity_refusal(job: DeletionJob, storage: StorageProvider) -> Optional[str]:
    """Why this job must not be erased, or None when its destination is verified.

    Fail-closed in every branch. The question is not "can I find a reason to refuse" but "has
    the destination been positively confirmed", and anything short of that is a refusal:

    * no identity recorded -- the job predates capture, and binding it to current
      configuration is the silent-erasure path this exists to close;
    * identity recorded but incomplete, e.g. `s3` with no bucket;
    * the live provider cannot state where it points, so there is nothing to compare against;
    * the two disagree;
    * or the job recorded an owner guard that this worker cannot assert.

    None of these can be fixed by retrying, and none of them is a failure of the object -- they
    are facts about configuration, which is why they escalate immediately and why the job stays
    claimable rather than being marked done or failed.

    The owner check is deliberately asymmetric, and the asymmetry is the part to review. A
    recorded owner that this worker cannot honour is a refusal: the job was created on the
    understanding that the destination would be proved to belong to that account, and dropping
    that proof silently is how a bucket re-created under new ownership gets a delete it will
    happily accept and ignore. A recorded NULL against a worker that now *has* a guard is not a
    refusal: the guard only narrows what the worker will accept, the service enforces it on
    every call, and holding the whole queue because an operator added a safety setting would
    punish adding one. `StorageIdentity.expected_owner` is excluded from `==` for exactly this
    reason -- the comparison belongs here, where the direction can be stated, rather than in a
    dataclass equality that can only say "different".
    """
    recorded = recorded_identity(job)
    if recorded is None:
        return IDENTITY_UNKNOWN
    if not recorded.is_complete:
        return IDENTITY_INCOMPLETE

    reader = getattr(storage, "identity", None)
    live = reader() if callable(reader) else None
    if live is None:
        return IDENTITY_UNDETERMINABLE
    if recorded != live:
        return IDENTITY_MISMATCH
    # Destination confirmed. The owner guard is compared separately because `==` above ignores
    # it on purpose; see the docstring for why the two directions are not symmetric.
    if recorded.expected_owner:
        if not live.expected_owner:
            return IDENTITY_OWNER_UNCONFIRMED
        if recorded.expected_owner != live.expected_owner:
            return IDENTITY_OWNER_MISMATCH
    return None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """SQLite hands back naive datetimes even from a timezone-aware column.

    Comparing one of those with an aware `now()` raises, so every value read back from the
    database passes through here. PostgreSQL returns aware values and this is a no-op.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=datetime.timezone.utc)


def backoff_for(attempts: int) -> datetime.timedelta:
    """Exponential, capped. Capped rather than unbounded, so a job stays visible."""
    if attempts <= 0:
        return BACKOFF_BASE
    grown = BACKOFF_BASE * (2 ** min(attempts, 16))
    return min(grown, BACKOFF_CAP)


@dataclass
class RunReport:
    """What one pass did. Counts and ids only -- nothing that identifies a person."""

    claimed: int = 0
    deleted: int = 0
    already_absent: int = 0
    retried: int = 0
    needs_attention: int = 0
    requests_closed: int = 0

    @property
    def resolved(self) -> int:
        return self.deleted + self.already_absent


def claim_batch(db: Session, *, limit: int = 10, now: Optional[datetime.datetime] = None) -> list[DeletionJob]:
    """Lease up to `limit` due jobs. One statement, `FOR UPDATE SKIP LOCKED`.

    `SKIP LOCKED`, not `NOWAIT`: a second worker must step over a row another worker holds and
    carry on to the next, rather than erroring out. `NOWAIT` would make concurrency an error
    condition; blocking would serialise the workers.

    A job is due when its status is unfinished, its `next_attempt_at` has passed, and its lease
    has expired or was never taken. `needs_attention` is included -- it is a flag, not an
    exit.

    On SQLite there is no `SKIP LOCKED` and no row-level locking, so the clause is omitted and
    the claim relies on the single-writer model. The PostgreSQL behaviour is what the
    concurrency tests exercise, on the real engine.
    """
    moment = now or _now()
    is_postgres = db.bind is not None and db.bind.dialect.name == "postgresql"

    statement = (
        select(DeletionJob.id)
        .where(
            DeletionJob.status.in_((STATUS_PENDING, STATUS_NEEDS_ATTENTION)),
            (DeletionJob.next_attempt_at.is_(None)) | (DeletionJob.next_attempt_at <= moment),
            (DeletionJob.locked_until.is_(None)) | (DeletionJob.locked_until <= moment),
        )
        .order_by(DeletionJob.next_attempt_at.asc().nulls_first(), DeletionJob.id.asc())
        .limit(limit)
    )
    if is_postgres:
        statement = statement.with_for_update(skip_locked=True)

    ids = [row[0] for row in db.execute(statement).all()]
    if not ids:
        return []

    # Stamp the lease in the same transaction as the lock, so the rows are unavailable to
    # another worker the moment this one commits -- and still locked until it does.
    claimed: list[DeletionJob] = []
    for job_id in ids:
        job = db.get(DeletionJob, job_id)
        if job is None:  # pragma: no cover - deleted between the select and the get
            continue
        job.locked_until = moment + LEASE
        job.updated_at = moment
        claimed.append(job)
    db.flush()
    return claimed


def _hold_for_operator(
    job: DeletionJob, *, error_class: str, moment: datetime.datetime
) -> str:
    """Record an explicit non-success and leave the job claimable.

    Deliberately not a terminal state. `needs_attention` means "still owed, and a human should
    look"; a `failed` status would convert an unfinished erasure into an abandoned one, which
    is what the outbox design forbids. The job keeps its backoff so a misconfigured worker does
    not spin, and it resumes by itself once the cause is fixed -- no operator action is needed
    beyond repairing the configuration.
    """
    job.attempts = (job.attempts or 0) + 1
    job.last_error_class = error_class
    job.next_attempt_at = moment + backoff_for(job.attempts)
    job.locked_until = None
    job.status = STATUS_NEEDS_ATTENTION
    job.updated_at = moment
    return STATUS_NEEDS_ATTENTION


def execute_job(db: Session, job: DeletionJob, storage: StorageProvider) -> str:
    """Erase one object. Returns "deleted", "retry" or "needs_attention".

    The storage call is made with no transaction open around it -- the caller commits the claim
    first -- because a transaction must never span a network call. If this process dies between
    the delete and the status write, the lease expires and the job is retried; deleting an
    object that is already gone is success, so the retry is harmless. That is the whole reason
    idempotence is a requirement rather than a nicety.

    The call is `erase`, not `delete`, and the difference is the point of this stage. `delete`
    promises only that the object is no longer current, which on a versioned bucket is
    satisfied by inserting a delete marker over data that is all still there. `erase` promises
    that no version and no delete marker remains, and proves it by enumerating the history
    after deleting each version by id -- so a `done` here rests on a verified end state rather
    than on a call that did not raise.

    An object that was already absent is still success: the obligation is "the object is not
    there", and `erase` establishes that by the same enumeration rather than by a 404, which
    would arrive while every version was intact.
    """
    moment = _now()

    expected = getattr(storage, "name", None)
    if expected is not None and job.storage_provider != expected:
        # The job has always recorded the backend it was enqueued against, and until now
        # nothing read it. Deleting the key from the wrong backend would almost always
        # *succeed*: the key is absent there, absence is idempotent success, so the job would
        # be marked done, and closing the request then deletes the last row that still held
        # the key. A wrong-provider delete is therefore not a failed erasure but a silent one,
        # which is the single outcome this whole design exists to prevent.
        #
        # Escalated on the first occurrence rather than after ATTENTION_AFTER_ATTEMPTS: a
        # mismatch is a configuration fault and cannot clear by being retried. The job stays
        # claimable, so it resumes by itself once a worker is pointed at the right backend.
        job.attempts = (job.attempts or 0) + 1
        job.last_error_class = "ProviderMismatch"
        job.next_attempt_at = moment + backoff_for(job.attempts)
        job.locked_until = None
        job.status = STATUS_NEEDS_ATTENTION
        job.updated_at = moment
        logger.error(
            "Deletion job %s was enqueued for storage provider %r but this worker is "
            "configured for %r; refusing to erase against the wrong backend. The job stays "
            "queued and nothing was deleted.",
            job.id,
            job.storage_provider,
            expected,
        )
        return STATUS_NEEDS_ATTENTION

    # The destination, not just the backend. The check above compares provider names, and that
    # is not enough: every S3-compatible vendor is `s3` here, and the bucket is read from
    # configuration when the provider is constructed. So a bucket changed after this job was
    # enqueued sends the delete at the wrong destination, where the key is absent, where
    # absence reads as idempotent success -- and the job is marked done having erased nothing.
    #
    # This runs before `storage.erase` is reachable at all, which is the property that
    # matters: an unverified destination cannot produce a destructive call and cannot produce a
    # `done`.
    refusal = identity_refusal(job, storage)
    if refusal is not None:
        logger.error(
            "Deletion job %s was not erased: %s. The recorded destination could not be "
            "confirmed against this worker's storage, so nothing was deleted. The job stays "
            "queued and resumes once the configuration matches what it was enqueued against.",
            job.id,
            refusal,
        )
        return _hold_for_operator(job, error_class=refusal, moment=moment)

    # Resolved rather than called straight, for the same reason `identity_refusal` reads
    # `identity` with getattr: a storage object that does not implement version-aware erasure
    # must hold its job for an operator, not raise an AttributeError out of this function and
    # abandon the rest of the batch to its leases. Real adapters always have it -- the base
    # class provides a refusing default -- so this covers a non-conforming object only.
    eraser = getattr(storage, "erase", None)
    if not callable(eraser):
        logger.error(
            "Deletion job %s was not erased: the configured storage cannot erase an object "
            "version history, so no erasure can be proved. Nothing was deleted and the job "
            "stays queued.",
            job.id,
        )
        return _hold_for_operator(job, error_class=ERASURE_NOT_IMPLEMENTED, moment=moment)

    job.attempts = (job.attempts or 0) + 1
    try:
        outcome = eraser(job.storage_key)
    except StorageError as exc:
        # The class and the backend's own error code, never the message: a provider's message
        # routinely contains the key and the bucket, while a code such as `AccessDenied` or
        # `SlowDown` says what kind of failure it was without naming anything. The job id and
        # the request are enough to find the row.
        code = getattr(exc, "code", None)
        job.last_error_class = f"{type(exc).__name__}:{code}" if code else type(exc).__name__
        job.next_attempt_at = moment + backoff_for(job.attempts)
        job.locked_until = None
        if code in IMMEDIATE_ATTENTION_CODES:
            # A missing bucket, an unprovable erasure, a lock being honoured, a denied
            # request: none of these is a fault in the object and none of them clears by
            # being retried, so each is escalated on the first occurrence rather than after
            # ATTENTION_AFTER_ATTEMPTS -- the same treatment a provider mismatch gets. The S3
            # adapter used to count NoSuchBucket as "already gone", which reported a completed
            # erasure against a bucket that does not exist.
            # `attempts` is not incremented here: it was already incremented before the call.
            job.status = STATUS_NEEDS_ATTENTION
            job.updated_at = moment
            logger.error(
                "Deletion job %s was not erased (%s); nothing was deleted and retrying will "
                "not change that, so it is held for an operator. The job stays queued and "
                "resumes once the cause is fixed.",
                job.id,
                job.last_error_class,
            )
            return STATUS_NEEDS_ATTENTION
        if job.attempts >= ATTENTION_AFTER_ATTEMPTS:
            job.status = STATUS_NEEDS_ATTENTION
            job.updated_at = moment
            logger.error(
                "Deletion job %s still failing after %s attempts (%s); it remains queued and "
                "will keep retrying",
                job.id,
                job.attempts,
                job.last_error_class,
            )
            return STATUS_NEEDS_ATTENTION
        job.status = STATUS_PENDING
        job.updated_at = moment
        logger.warning(
            "Deletion job %s attempt %s failed (%s); retrying",
            job.id,
            job.attempts,
            job.last_error_class,
        )
        return "retry"

    # Only reachable from a verified outcome: `erase` raises rather than returning one it
    # could not confirm. The evidence is logged because it is the difference between an
    # enumeration that found nothing left and a 404 -- counts and provider facts only, no key
    # and no bucket.
    logger.info(
        "Deletion job %s erased its object: %s bucket, %s version(s) and %s delete marker(s) "
        "removed, absence established by %s",
        job.id,
        outcome.versioning,
        outcome.versions_deleted,
        outcome.markers_deleted,
        outcome.verified_by,
    )
    job.status = STATUS_DONE
    job.completed_at = moment
    job.updated_at = moment
    job.locked_until = None
    job.last_error_class = None
    return "deleted"


def close_request_if_finished(db: Session, request_id: int) -> bool:
    """Delete the final user row and close the request, if nothing is left.

    The outstanding count is taken **inside this transaction**, immediately before the user row
    is deleted, so a job created or reopened concurrently cannot be stepped over. Without that
    re-count, two workers finishing their last jobs at the same time could each see "none left"
    from a stale read.

    The user row is deleted here and not in Phase 1 because, until this point, the request is
    something an operator can reason about. Note what this does *not* depend on: the bucket.
    By the time this runs every object is already resolved, and if storage were unreachable
    the jobs would still be pending and this would simply not fire -- it never calls storage,
    so a bucket outage cannot keep the user row alive once the objects are gone.
    """
    outstanding = (
        db.query(DeletionJob)
        .filter(
            DeletionJob.deletion_request_id == request_id,
            DeletionJob.status.in_(UNRESOLVED_STATUSES),
        )
        .count()
    )
    if outstanding:
        return False

    request = db.get(DeletionRequest, request_id)
    if request is None or request.status == "done":
        return False

    moment = _now()
    user = db.get(User, request.original_user_id)
    if user is not None:
        db.delete(user)
    request.status = "done"
    request.completed_at = moment
    request.updated_at = moment
    logger.info("Deletion request %s completed", request.request_uuid)
    return True


def claim_finalizable_requests(db: Session, *, limit: int = 10) -> list[int]:
    """Unresolved requests with no unresolved job left, leased to this worker.

    The recovery half of the close, and the reason it has to exist: every other stage of this
    pipeline recovers by itself, and the close did not. `claim_batch` only ever selects jobs
    that are still pending or needs_attention, so once the final job committed `done` the
    request had no claimable job left -- and the only caller of
    `close_request_if_finished` was a loop over the requests whose job had transitioned in
    *that* pass. A process death between the last job commit and the close therefore left the
    request open permanently: still counted by `unresolved_obligations`, still logged by
    `report_sla_breaches`, and reachable by no code path at all. Only a human could finish it.

    The predicate is "no unresolved job", not "has a done job", which keeps the invariant the
    right way round: a request is finalizable because nothing is outstanding, so `pending` and
    `needs_attention` both hold it open, and a job that is reopened between this query and the
    close is caught by the re-count inside `close_request_if_finished`.

    A request with no jobs at all also qualifies, and that is correct rather than an oversight:
    `deletion_outbox.enqueue` inserts the request and its jobs in one transaction, so no other
    transaction can observe a request before its jobs exist. A visible request with no jobs has
    no obligation to wait for. (Phase 1 does not create one today -- `account_service` only
    opens a request when there is at least one key -- so this is a safety property, not a path.)

    `FOR UPDATE SKIP LOCKED`, exactly as `claim_batch` does it and for the same reason: two
    workers sweeping at once must not both finalize the same request, and the second must step
    over a locked row rather than block or error. The lock is held until the caller commits, by
    which point the winner has set the status to `done` and the row no longer matches. On SQLite
    the clause is omitted and the single-writer model stands in, the same compromise `claim_batch`
    documents; the concurrent case is exercised on the real engine.

    Bounded by `limit` so this is never a scan of the whole queue, and the rows are only read
    here -- the close itself happens one request per transaction in the caller, so no storage
    call or long-running transaction is introduced.
    """
    outstanding = (
        select(DeletionJob.id)
        .where(
            DeletionJob.deletion_request_id == DeletionRequest.id,
            DeletionJob.status.in_((STATUS_PENDING, STATUS_NEEDS_ATTENTION)),
        )
        .exists()
    )
    statement = (
        select(DeletionRequest.id)
        .where(DeletionRequest.status.in_(UNRESOLVED_STATUSES), ~outstanding)
        .order_by(DeletionRequest.id.asc())
        .limit(limit)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    return [row[0] for row in db.execute(statement).all()]


def overdue_requests(
    db: Session,
    *,
    threshold: datetime.timedelta,
    now: Optional[datetime.datetime] = None,
) -> Sequence[DeletionRequest]:
    """Open requests older than `threshold`, measured from `created_at`.

    `created_at` is set in the Phase 1 transaction, so the clock starts when the user's access
    was revoked rather than when a worker first looked -- which is the only reading that makes
    the 1-hour and 24-hour targets mean anything to the person waiting.
    """
    moment = now or _now()
    rows = (
        db.query(DeletionRequest)
        .filter(DeletionRequest.status.in_(UNRESOLVED_STATUSES))
        .all()
    )
    return [row for row in rows if (created := _aware(row.created_at)) and moment - created >= threshold]


def report_sla_breaches(db: Session, *, now: Optional[datetime.datetime] = None) -> dict[str, list[str]]:
    """Log and return the request uuids past each provisional target.

    Returns uuids rather than user ids: by this point the user row may be gone, and the uuid is
    the handle that still resolves. Targets are provisional, pending privacy and legal review.
    """
    moment = now or _now()
    escalate = [r.request_uuid for r in overdue_requests(db, threshold=ESCALATE_AFTER, now=moment)]
    alert = [
        r.request_uuid
        for r in overdue_requests(db, threshold=ALERT_AFTER, now=moment)
        if r.request_uuid not in escalate
    ]
    for uuid_value in escalate:
        logger.error(
            "Deletion request %s is open past the 24-hour escalation target (provisional, "
            "pending privacy/legal review); a person should look at it",
            uuid_value,
        )
    for uuid_value in alert:
        logger.error(
            "Deletion request %s is open past the 1-hour alert target (provisional, pending "
            "privacy/legal review)",
            uuid_value,
        )
    return {"alert": alert, "escalate": escalate}


def run_once(db: Session, storage: StorageProvider, *, limit: int = 10) -> RunReport:
    """One pass: claim, erase, close. Safe to run concurrently with itself.

    The transaction boundaries are deliberate, and are the part to review:

    1. claim and commit -- the lease is durable before any object is touched, so a crash
       cannot leave a job both unclaimed and half-done;
    2. one storage call per job, with **no transaction open**;
    3. one transaction per job to record the outcome, so one job's failure cannot roll back
       another job's success;
    4. a final transaction per affected request to re-count and close;
    5. a bounded recovery sweep for requests no longer reachable through their jobs, one
       transaction each.

    Boundary 5 is what makes the close crash-safe. It deliberately runs even when this pass
    claimed nothing, because a request orphaned by a crash is in exactly that state: every job
    `done`, nothing claimable, and so previously nothing that would ever look at it again.
    """
    report = RunReport()

    jobs = claim_batch(db, limit=limit)
    report.claimed = len(jobs)
    # Boundary 1: the lease is durable before anything is erased. Committed even for an empty
    # claim, which also ends the read transaction before the sweep below opens its own.
    db.commit()

    touched_requests: set[int] = set()
    for job in jobs:
        request_id = job.deletion_request_id
        # Boundary 2 and 3: the storage call happens inside execute_job with nothing open; the
        # commit that follows records only this job's outcome.
        outcome = execute_job(db, job, storage)
        db.commit()
        if outcome == "deleted":
            report.deleted += 1
            touched_requests.add(request_id)
        elif outcome == STATUS_NEEDS_ATTENTION:
            report.needs_attention += 1
        else:
            report.retried += 1

    # Boundary 4: re-count per request, then close.
    for request_id in sorted(touched_requests):
        if close_request_if_finished(db, request_id):
            report.requests_closed += 1
        db.commit()

    # Boundary 5: finish anything left behind by an earlier process. Requests closed above are
    # already `done`, so they no longer match the claim and are not counted twice -- and
    # `close_request_if_finished` returns False for an already-closed request in any case.
    # One transaction per request, so a request that cannot be closed cannot roll back one that
    # was, and nothing here calls storage.
    for request_id in claim_finalizable_requests(db, limit=limit):
        if close_request_if_finished(db, request_id):
            report.requests_closed += 1
        db.commit()

    report_sla_breaches(db)
    return report


def claimable_count(db: Session, *, now: Optional[datetime.datetime] = None) -> int:
    """How many jobs are due. For monitoring the 5-minute pickup target."""
    moment = now or _now()
    return (
        db.query(DeletionJob)
        .filter(
            DeletionJob.status.in_((STATUS_PENDING, STATUS_NEEDS_ATTENTION)),
            (DeletionJob.next_attempt_at.is_(None)) | (DeletionJob.next_attempt_at <= moment),
        )
        .count()
    )


def unresolved_obligations(db: Session) -> int:
    """Open requests. What the downgrade guard asks about, phrased for callers."""
    return (
        db.query(DeletionRequest)
        .filter(DeletionRequest.status.in_(UNRESOLVED_STATUSES))
        .count()
    )


@dataclass
class IdentityReview:
    """What the queue looks like from a storage-identity point of view.

    Counts, job ids and configuration fingerprints. No storage key, no bucket, no endpoint:
    the same rule the rest of this module follows, so the report can be pasted into an
    incident channel. The fingerprint is what makes that possible -- it groups jobs by the
    configuration they were enqueued under without naming it.
    """

    unresolved_jobs: int = 0
    #: Jobs whose identity was never captured. These predate identity capture and are held.
    without_identity: list[int] = field(default_factory=list)
    #: Jobs with an identity that is recorded but unusable, e.g. `s3` with no bucket.
    incomplete_identity: list[int] = field(default_factory=list)
    #: Unresolved jobs per configuration fingerprint, newest configuration not distinguished.
    by_revision: dict[str, int] = field(default_factory=dict)
    #: Job ids per `last_error_class`, for the ones currently flagged for a human.
    held_by_reason: dict[str, list[int]] = field(default_factory=dict)

    @property
    def needs_operator(self) -> int:
        """Jobs that cannot proceed without a configuration change."""
        return len(self.without_identity) + len(self.incomplete_identity)


def review_identities(db: Session) -> IdentityReview:
    """Read-only survey of unresolved jobs and their recorded destinations.

    **Deletes nothing, writes nothing and opens no storage connection.** It exists because the
    fail-closed rule in `identity_refusal` is only humane if an operator can see what is being
    held and why: a job created before identity capture will sit in `needs_attention` for ever
    otherwise, and the queue would look broken rather than deliberately cautious.

    Why not resolve the held jobs automatically. The only identity available to assign is the
    one this process is configured with, and if that is wrong the object becomes unreachable
    the moment the job is marked done -- the request closes and the last row holding the key is
    deleted. An operator who can compare the fingerprints to their own deployment history can
    make that call; this function cannot.
    """
    jobs: Sequence[DeletionJob] = (
        db.query(DeletionJob).filter(DeletionJob.status.in_(UNRESOLVED_STATUSES)).all()
    )
    review = IdentityReview(unresolved_jobs=len(jobs))
    for job in jobs:
        identity = recorded_identity(job)
        if identity is None:
            review.without_identity.append(job.id)
        else:
            if not identity.is_complete:
                review.incomplete_identity.append(job.id)
            revision = job.storage_config_revision or "unrecorded"
            review.by_revision[revision] = review.by_revision.get(revision, 0) + 1
        if job.status == STATUS_NEEDS_ATTENTION and job.last_error_class:
            review.held_by_reason.setdefault(job.last_error_class, []).append(job.id)
    return review
