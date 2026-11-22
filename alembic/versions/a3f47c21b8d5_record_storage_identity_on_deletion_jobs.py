"""record storage identity on deletion jobs

Four additive, nullable columns on `deletion_jobs`. No existing column is altered, no type is
changed, nothing is dropped, and **there is deliberately no backfill**.

Why no backfill. A job created before this migration has no recorded destination. The only
value available to fill it with is the configuration this process happens to hold now, and
writing that in would assert something nobody verified -- that the bucket configured today is
the bucket the object was written to. If it is not, the worker deletes a key that is absent
from the wrong bucket, S3 reports NoSuchKey, the adapter treats absence as idempotent success,
the job is marked done, and closing the request destroys the last row that held the key. The
erasure then never happens and nothing anywhere records that it did not. Leaving these NULL is
what lets `app/workers/deletion_worker.py` recognise such a job and hold it for an operator
instead of acting on a guess.

`storage_config_revision` is therefore load-bearing rather than cosmetic: NULL means "identity
was never captured for this job", which the worker treats differently from "identity does not
match".

No credential is stored by this migration. The columns record *where* an object is -- provider,
bucket, endpoint, region -- never how to reach it. Access key ids and secrets are excluded by
design in `app/storage/identity.py`.

Rollback implication, stated because it is not obvious: `downgrade()` drops the identity, so
every still-unresolved job becomes one with no recorded destination. That is safe -- the jobs
survive and the worker holds them rather than erasing anything -- but it is not free, because
the captured identity cannot be recovered by upgrading again. It has to be re-established by an
operator. The outbox's own `downgrade()` guard is not invoked here: that guard exists for
dropping the rows that *are* the obligation, and this drops none of them.

Revision ID: a3f47c21b8d5
Revises: f7b3c91de204
Create Date: 2026-11-22

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a3f47c21b8d5"
down_revision: Union[str, None] = "f7b3c91de204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("deletion_jobs", sa.Column("storage_bucket", sa.String(length=255), nullable=True))
    op.add_column("deletion_jobs", sa.Column("storage_endpoint", sa.String(length=255), nullable=True))
    op.add_column("deletion_jobs", sa.Column("storage_region", sa.String(length=64), nullable=True))
    op.add_column(
        "deletion_jobs", sa.Column("storage_config_revision", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    # batch_alter_table rather than bare op.drop_column, following the precedent set in
    # 75a38441b414: a plain ALTER TABLE ... DROP COLUMN is not supported by every SQLite
    # version this project is developed against, and batch mode rebuilds the table instead.
    with op.batch_alter_table("deletion_jobs") as batch_op:
        batch_op.drop_column("storage_config_revision")
        batch_op.drop_column("storage_region")
        batch_op.drop_column("storage_endpoint")
        batch_op.drop_column("storage_bucket")
