"""record the expected bucket owner on deletion jobs

One additive, nullable column on `deletion_jobs`. Nothing is altered, dropped or backfilled.

Why a fifth identity field, when a3f47c21b8d5 already recorded four. Provider, bucket,
endpoint and region say where an object is; none of them says who owns that place. An S3
bucket name is globally unique only while it exists -- delete it and another account can take
the name -- and an S3-compatible endpoint can serve the same bucket name to two tenants. In
either case a delete sent to a destination whose four location fields match exactly would be
accepted by a stranger's bucket, find the key absent, and report an idempotent success, which
marks the job done and destroys the last row holding the key. That is the same silent-erasure
failure a3f47c21b8d5 closed, one level further out.

The column exists so the guard travels with the job. The worker sends it as
`ExpectedBucketOwner`, which means the check is enforced by the service -- a 403 from S3 --
rather than by this process comparing configuration with itself.

NULL is a normal, supported value and does not mean "unverified". It means no owner guard was
configured when the job was created, which is the state of every job created before this
migration and of every deployment that does not set STORAGE_S3_EXPECTED_BUCKET_OWNER. Those
jobs keep their destination check unchanged; see `deletion_worker.identity_refusal` for the
asymmetry, which is deliberate: a recorded owner that the worker cannot honour is a refusal,
while a recorded NULL against a worker that now has a guard is not, because adding safety must
not freeze the queue.

No credential is stored. An account id grants no access on its own. It is still excluded from
logs, from `StorageIdentity.describe()` and from the operator report, because it identifies an
account.

Rollback implication: `downgrade()` drops the column, so unresolved jobs lose their recorded
owner and fall back to the NULL case above -- destination checks continue, the owner guard
stops being asserted locally, and nothing is erased that would not have been. The jobs
themselves are untouched, so the outbox downgrade guard does not apply here.

Revision ID: c5d81e0a9f36
Revises: a3f47c21b8d5
Create Date: 2026-11-23

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c5d81e0a9f36"
down_revision: Union[str, None] = "a3f47c21b8d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deletion_jobs", sa.Column("storage_expected_owner", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    # batch_alter_table rather than bare op.drop_column, following 75a38441b414 and
    # a3f47c21b8d5: a plain ALTER TABLE ... DROP COLUMN is not supported by every SQLite
    # version this project is developed against, and batch mode rebuilds the table instead.
    with op.batch_alter_table("deletion_jobs") as batch_op:
        batch_op.drop_column("storage_expected_owner")
