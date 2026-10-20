"""Phase 2 of account deletion: erase the objects, then the last user row.

Access was already revoked in Phase 1, so nothing here is urgent for the user's safety -- what
it is responsible for is finishing. The design constraints, all from
docs/proposal-account-deletion-outbox.md:

* **Every job is idempotent.** Deleting an object that is already gone is success, because the
  obligation is "the object is not there", not "this process removed it".
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
from dataclasses import dataclass
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


def execute_job(db: Session, job: DeletionJob, storage: StorageProvider) -> str:
    """Erase one object. Returns "deleted", "retry" or "needs_attention".

    The storage call is made with no transaction open around it -- the caller commits the claim
    first -- because a transaction must never span a network call. If this process dies between
    the delete and the status write, the lease expires and the job is retried; deleting an
    object that is already gone is success, so the retry is harmless. That is the whole reason
    idempotence is a requirement rather than a nicety.

    `StorageProvider.delete` already treats an absent object as success, so "already absent"
    and "deleted" arrive here identically; the distinction in the report comes from the
    provider, not from a second existence check that would race anyway.
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

    job.attempts = (job.attempts or 0) + 1
    try:
        storage.delete(job.storage_key)
    except StorageError as exc:
        # The class and the backend's own error code, never the message: a provider's message
        # routinely contains the key and the bucket, while a code such as `AccessDenied` or
        # `SlowDown` says what kind of failure it was without naming anything. The job id and
        # the request are enough to find the row.
        code = getattr(exc, "code", None)
        job.last_error_class = f"{type(exc).__name__}:{code}" if code else type(exc).__name__
        job.next_attempt_at = moment + backoff_for(job.attempts)
        job.locked_until = None
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
    4. a final transaction per affected request to re-count and close.
    """
    report = RunReport()

    jobs = claim_batch(db, limit=limit)
    report.claimed = len(jobs)
    if not jobs:
        db.commit()
        report_sla_breaches(db)
        return report

    # Boundary 1: the lease is durable before anything is erased.
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
