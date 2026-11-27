"""Report which resumes have no usable record of where their file is stored.

**This script deletes nothing, writes nothing, and opens no storage connection.** It reads the
`resumes` table and prints counts, resume ids and configuration fingerprints. Run it as often
as you like; it cannot change a row.

## Why it exists

`resumes.file_path` is only meaningful to the storage that holds it, and
`get_storage_provider()` builds a provider from current settings on every request. Since
migration b2e7f14ac903 each resume records where its file was actually written, and every
object operation -- download, the ATS PDF read, deletion, and the key handed to an
account-deletion job -- refuses unless that recorded destination matches the live provider.

The rule is fail-closed, so two situations produce a refusal without anything being broken:

* a resume uploaded before identity capture existed, which has no recorded destination at all;
  and
* a resume uploaded under a configuration this process is no longer pointed at.

From the outside both look like one student's resume failing to download or delete with a 500.
This is the surface that says which resumes, how many, and which of the two it is.

## What the output does not contain

No storage key, no bucket name, no endpoint, no account id, no credential. Keys are user-scoped
paths and are personal data by association, and buckets and endpoints are withheld from logs
throughout this codebase for the same reason. Resumes are grouped by a short fingerprint of
their recorded destination instead, which distinguishes one configuration from another without
naming either, so the output is safe to paste into an incident channel.

Resume ids are printed because an operator needs them to act. A resume id identifies a row, not
a person, and nothing here reveals the student, the filename or the contents.

## Reading the result

`without identity` are resumes that predate capture: nothing says which bucket holds the file.
`incomplete identity` are resumes whose destination was recorded but is unusable -- an `s3`
resume with no bucket, for instance. The fingerprint table groups the rest: more than one
fingerprint means resumes were uploaded under more than one storage configuration, and only one
of them can match this process now. The fingerprint matching the live configuration is printed
separately, so the rows that do work are distinguishable from the rows that do not.

## What to do about a refused resume

Nothing in this script, and nothing by hand in the database without reading the runbook first.
A resume under a fingerprint that is simply not the live one is fixed by pointing the service at
the storage those uploads went to; the refusal is stateless, so it resolves the moment
configuration matches, with no re-queueing step.

A resume with no recorded destination needs a human decision, and it is a real one: the only
value available to stamp automatically is this process's current configuration, and if that is
wrong, the object becomes permanently unreachable the moment a deletion marks it done -- because
deleting the row destroys the last pointer to the key. Deciding where an old upload went means
comparing this report against deployment history, which is why no flag is offered here that
assigns a destination.

Deliberately not offered: a `--backfill-current` of any kind. See
`docs/runbook-deletion-worker.md` for the same reasoning applied to deletion jobs, and for the
recovery procedure.

Usage:

    python scripts/review_resume_identity.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.session import SessionLocal  # noqa: E402
from app.models.resume import Resume  # noqa: E402
from app.storage.identity import StorageIdentity, recorded_identity  # noqa: E402


def _ids(ids: list[int], limit: int = 40) -> str:
    """Resume ids, truncated so a large backlog does not fill a terminal."""
    shown = ", ".join(str(i) for i in ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, ... (+{len(ids) - limit} more)"


def main() -> int:
    # The live destination, for comparison only. Read once here rather than per row: it cannot
    # change under us mid-report, and reading it repeatedly would invite the mistake this whole
    # mechanism exists to prevent -- treating current configuration as evidence about an object.
    live = StorageIdentity.from_settings()

    with SessionLocal() as db:
        total = db.query(Resume).count()
        rows = db.query(
            Resume.id,
            Resume.storage_provider,
            Resume.storage_bucket,
            Resume.storage_endpoint,
            Resume.storage_region,
            Resume.storage_expected_owner,
            Resume.storage_config_revision,
        ).all()

    without_identity: list[int] = []
    incomplete: list[int] = []
    by_revision: Counter[str] = Counter()
    matching_live = 0

    for row in rows:
        identity = recorded_identity(row)
        if identity is None:
            without_identity.append(row.id)
            continue
        if not identity.is_complete:
            incomplete.append(row.id)
            continue
        by_revision[identity.revision] += 1
        if identity == live:
            matching_live += 1

    needing_attention = len(without_identity) + len(incomplete) + (
        sum(by_revision.values()) - matching_live
    )

    print("Resume storage identity review (read-only)")
    print(f"  resumes ..................... {total}")
    print(f"  usable from this process .... {matching_live}")
    print(f"  needing attention ........... {needing_attention}")
    print(f"  live configuration .......... {live.describe()}")

    if without_identity:
        print(f"\n  resumes with no recorded destination ({len(without_identity)}):")
        print(f"    ids: {_ids(without_identity)}")
        print("    These predate identity capture. Their files have not been touched and")
        print("    nothing has been erased. Downloads, ATS PDF reads and deletions are refused")
        print("    for them. Read the runbook before assigning a destination -- guessing makes")
        print("    the object unreachable rather than deleted.")

    if incomplete:
        print(f"\n  resumes with an unusable destination ({len(incomplete)}):")
        print(f"    ids: {_ids(incomplete)}")
        print("    A destination was recorded but does not say enough to find the object, for")
        print("    example an s3 resume with no bucket.")

    if by_revision:
        print("\n  resumes by configuration fingerprint:")
        for revision, count in sorted(by_revision.items(), key=lambda kv: -kv[1]):
            marker = "  <- live" if revision == live.revision else ""
            print(f"    {revision} .... {count}{marker}")
        if len(by_revision) > 1:
            print("    More than one fingerprint: these resumes were uploaded under different")
            print("    storage configurations, and this process can only match one at a time.")
            print("    The others are refused until a process is pointed at their storage.")

    if not needing_attention:
        print("\n  Nothing outstanding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
