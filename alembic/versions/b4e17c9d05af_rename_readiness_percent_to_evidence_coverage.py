"""rename readiness_snapshots.readiness_percent to evidence_coverage_percent

Phase 2G. The column never held readiness. It holds the weighted mean of evidence confidence
across a role's skills -- the quantity Career Twin computes and Career Decision Lab shows.
Readiness in this product means JobReadinessRead.overall_percent, a five-signal average, and
one word naming two formulas is the semantic lie this phase exists to remove.

This is a RENAME and nothing else. The values are historical measurements taken on real days
and are carried across untouched: no recomputation, no backfill, no transformation, no
nullability change. A 0 in this column means a student was genuinely measured at 0% coverage
on that date, and it must still read 0 afterwards.

The table name stays `readiness_snapshots`. It names a table, not a measurement, and renaming
it would turn a single-column change into a full table rebuild with no honesty gained.

batch_alter_table is used because SQLite cannot ALTER a column in place; on PostgreSQL the
same call emits a plain ALTER TABLE ... RENAME COLUMN. Both are supported environments here.

Revision ID: b4e17c9d05af
Revises: 9a2f7be31c04
Create Date: 2026-09-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4e17c9d05af"
down_revision: Union[str, None] = "9a2f7be31c04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("readiness_snapshots", schema=None) as batch_op:
        batch_op.alter_column(
            "readiness_percent",
            new_column_name="evidence_coverage_percent",
            existing_type=sa.Integer(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("readiness_snapshots", schema=None) as batch_op:
        batch_op.alter_column(
            "evidence_coverage_percent",
            new_column_name="readiness_percent",
            existing_type=sa.Integer(),
            existing_nullable=False,
        )
