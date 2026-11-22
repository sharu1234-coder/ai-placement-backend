"""Report which account-deletion jobs are being held because their destination is unconfirmed.

**This script deletes nothing, writes nothing, and opens no storage connection.** It reads the
`deletion_jobs` table and prints counts, job ids and configuration fingerprints. Run it as
often as you like; it cannot change the queue.

## Why it exists

`app/workers/deletion_worker.py` refuses to erase an object unless the destination recorded on
the job matches the storage the worker holds. That rule is deliberately fail-closed: a job
whose destination cannot be confirmed is flagged `needs_attention` and left claimable rather
than erased or abandoned. Two situations produce that outcome without anything being broken:

* a job created before identity capture existed, which has no recorded destination at all; and
* a job created under a configuration the worker is no longer pointed at.

Both are invisible from counters alone -- the queue simply stops draining -- so this is the
surface that says which jobs, how many, and why.

## What the output does not contain

No storage key, no bucket name, no endpoint, no credential. Keys are user-scoped paths and are
personal data by association, and the worker withholds buckets and endpoints from its logs for
the same reason. Jobs are grouped by a short fingerprint of their recorded destination instead,
which distinguishes one configuration from another without naming either, so the output is
safe to paste into an incident channel.

## Reading the result

`without identity` are jobs that predate identity capture. `incomplete identity` are jobs whose
destination was recorded but is unusable -- an `s3` job with no bucket, for instance. The
fingerprint table groups the rest: several fingerprints means jobs were enqueued under more
than one storage configuration, and only one of them can match the worker now.

## What to do about a held job

Nothing in this script, and nothing by hand in the database. Point a worker at the storage the
jobs were enqueued against and they resume on their own -- the refusal keeps the job claimable
precisely so that no re-queueing step is needed. The recovery procedure, including how to
decide which configuration a fingerprint refers to, is in `docs/runbook-deletion-worker.md`.

Deliberately not offered here: a flag that stamps the current configuration onto jobs that have
none. The only value available to stamp is this process's own, and if it is wrong the object
becomes permanently unreachable the moment the job is marked done, because closing the request
deletes the last row that held its key. That decision needs a human who can compare
fingerprints against deployment history.

Usage:

    python scripts/review_deletion_identity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.session import SessionLocal  # noqa: E402
from app.workers import deletion_worker  # noqa: E402


def main() -> int:
    with SessionLocal() as db:
        review = deletion_worker.review_identities(db)
        obligations = deletion_worker.unresolved_obligations(db)

    print("Account-deletion identity review (read-only)")
    print(f"  unresolved requests ........ {obligations}")
    print(f"  unresolved jobs ............ {review.unresolved_jobs}")
    print(f"  needing an operator ........ {review.needs_operator}")

    if review.without_identity:
        print(f"\n  jobs with no recorded destination ({len(review.without_identity)}):")
        print(f"    ids: {_ids(review.without_identity)}")
        print("    These predate identity capture. They are held, not failed, and nothing has")
        print("    been erased for them. See the runbook before assigning a destination.")

    if review.incomplete_identity:
        print(f"\n  jobs with an unusable destination ({len(review.incomplete_identity)}):")
        print(f"    ids: {_ids(review.incomplete_identity)}")

    if review.by_revision:
        print("\n  unresolved jobs by configuration fingerprint:")
        for revision, count in sorted(review.by_revision.items(), key=lambda kv: -kv[1]):
            print(f"    {revision} .... {count}")
        if len(review.by_revision) > 1:
            print("    More than one fingerprint: these jobs were enqueued under different")
            print("    storage configurations, and a worker can only match one at a time.")

    if review.held_by_reason:
        print("\n  jobs flagged for attention, by reason:")
        for reason, ids in sorted(review.held_by_reason.items()):
            print(f"    {reason} ({len(ids)}): {_ids(ids)}")

    if not review.unresolved_jobs:
        print("\n  Nothing outstanding.")
    return 0


def _ids(ids: list[int], limit: int = 40) -> str:
    """Job ids, truncated so a large backlog does not fill a terminal."""
    shown = ", ".join(str(i) for i in ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, ... (+{len(ids) - limit} more)"


if __name__ == "__main__":
    raise SystemExit(main())
