"""record storage identity on resumes

Six additive, nullable columns on `resumes`. Nothing is altered, dropped or backfilled.

## Why

`a3f47c21b8d5` and `c5d81e0a9f36` recorded a destination on `deletion_jobs`, which closed the
window between an account-deletion request being made and the worker erasing the objects: the
job remembers where its object was, so reconfiguring storage while jobs are queued can no
longer send an erasure to the wrong bucket, where the key is absent, absence is idempotent
success, and the last row holding the key is then deleted.

The window this closes is the earlier one, and it is far wider. `resumes.file_path` is written
at upload time and read back whenever the student downloads the resume, runs an ATS check, or
deletes it -- days or months later, through a provider that `get_storage_provider()` builds
from *current* settings on every request. Nothing recorded where the file was actually put, so:

* a read after a bucket change fails for no apparent reason, or worse, succeeds against an
  unrelated object that happens to share the key;
* an erasure reports success having deleted nothing, while the original object remains in the
  old bucket -- the same silent-erasure failure as above, one stage further upstream;
* and `deletion_outbox.collect_storage_keys` stamped every one of a profile's keys with
  `StorageIdentity.from_settings()`, so the destination a job carried was the configuration at
  *deletion* time, not at upload time. The job-level guard then compared that value against
  itself and confirmed it. The guard was real; what it verified was not.

The columns are the same six as `deletion_jobs` on purpose, so one duck-typed reader in
`app/storage/identity.py` serves both and the "was an identity captured" question has a single
answer.

## Nullable, and deliberately not backfilled

Every existing resume gets NULL. That is the honest value: this process cannot know which
bucket an old upload went to, and stamping the current configuration onto those rows would
manufacture exactly the false confirmation described above -- with the added cost that a wrong
value is indistinguishable from a right one afterwards.

Those rows therefore fail closed. A download, an ATS PDF read or a deletion of a resume with
no recorded identity is refused rather than guessed, account deletion enqueues its job with a
NULL revision, which the worker already holds for an operator, and
`scripts/review_resume_identity.py` reports which rows need recovery. Recovery is a human
decision made against deployment history, which is why no flag is offered that assigns a
destination automatically.

Deployments that have never changed storage configuration see no behavioural change for new
uploads and a refusal only on resumes uploaded before this migration.

## No credential is stored

These six say *where* an object is, never how to reach it. `storage_expected_owner` names an
account, which grants no access on its own, and is excluded from logs, from
`StorageIdentity.describe()` and from the operator report for the same reason the bucket is.

## Rollback implication

`downgrade()` drops the columns, so every resume reverts to having no recorded destination.
Reads, ATS PDF loads and deletions then refuse for *all* resumes rather than silently using
current configuration -- fail-closed in both directions, which is the intended behaviour of a
rollback here, but it does mean resume deletion stops working until either the upgrade is
reapplied or an operator intervenes. Nothing is erased that would not have been, and no object
becomes unreachable: `file_path` is untouched.

Revision ID: b2e7f14ac903
Revises: c5d81e0a9f36
Create Date: 2026-11-27

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b2e7f14ac903"
down_revision: Union[str, None] = "c5d81e0a9f36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("storage_provider", sa.String(length=20), nullable=True))
    op.add_column("resumes", sa.Column("storage_bucket", sa.String(length=255), nullable=True))
    op.add_column("resumes", sa.Column("storage_endpoint", sa.String(length=255), nullable=True))
    op.add_column("resumes", sa.Column("storage_region", sa.String(length=64), nullable=True))
    op.add_column(
        "resumes", sa.Column("storage_expected_owner", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "resumes", sa.Column("storage_config_revision", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    # batch_alter_table rather than bare op.drop_column, following 75a38441b414, a3f47c21b8d5
    # and c5d81e0a9f36: a plain ALTER TABLE ... DROP COLUMN is not supported by every SQLite
    # version this project is developed against, and batch mode rebuilds the table instead.
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.drop_column("storage_config_revision")
        batch_op.drop_column("storage_expected_owner")
        batch_op.drop_column("storage_region")
        batch_op.drop_column("storage_endpoint")
        batch_op.drop_column("storage_bucket")
        batch_op.drop_column("storage_provider")
