"""add grounded sections to job preparation plans

Revision ID: e8691c85f409
Revises: fb4bb77f3d17
Create Date: 2026-09-17 14:11:35.430380

Three nullable JSON columns on job_preparation_plans, holding the sections the unified Job
Match & Prep result added: per-gap guidance, JD-specific resume recommendations, and the
connections between a JD requirement and the student's own projects.

Purely additive. No existing column is renamed, retyped or dropped, no data is backfilled, and
no other table is touched -- applying this to a live database changes nothing about how
existing plans read.

Nullable rather than NOT NULL DEFAULT '[]' on purpose. A plan saved before this migration
genuinely has no guidance, which is a different fact from a plan that was generated and
produced none; NULL says "this section predates the feature" and the API reports it as absent,
while an empty list would claim we looked and found nothing to say.

JSON (not JSONB) matches every other JSON column in this schema, including the sibling columns
on this same table -- these are read back whole and never queried by their internal structure,
so JSONB's indexing would buy nothing and would make this table inconsistent with itself.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8691c85f409"
down_revision: Union[str, None] = "fb4bb77f3d17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "job_preparation_plans"
_COLUMNS = ("gap_guidance", "resume_recommendations", "project_connections")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(_TABLE, sa.Column(column, sa.JSON(), nullable=True))


def downgrade() -> None:
    # Reverse order, mirroring upgrade. Dropping these discards any generated guidance,
    # recommendations and project connections -- every one of which is regenerable by running
    # the preparation plan again, so nothing irreplaceable is lost.
    for column in reversed(_COLUMNS):
        op.drop_column(_TABLE, column)
