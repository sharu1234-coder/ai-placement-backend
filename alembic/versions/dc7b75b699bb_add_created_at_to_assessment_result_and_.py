"""add created_at to assessment_result and interview_evaluation

Revision ID: dc7b75b699bb
Revises: 0fcbe16e48ba
Create Date: 2026-08-23 14:48:42.337667

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'dc7b75b699bb'
down_revision: Union[str, None] = '0fcbe16e48ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate produced a NOT NULL column with no default, which fails against any
    # table that already has rows (both SQLite and Postgres reject a NOT NULL ADD COLUMN
    # with no way to fill existing rows). server_default backfills existing rows at the DB
    # level; the ORM model's Python-side `default=` still sets it going forward, so the
    # server_default is really only exercised by this migration.
    op.add_column(
        'assessment_results',
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.add_column(
        'interview_evaluations',
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
    )


def downgrade() -> None:
    # batch_alter_table: bare op.drop_column() emits a plain ALTER TABLE ... DROP COLUMN,
    # which SQLite only supports natively from 3.35+ -- older SQLite (still in use by some
    # dev/CI environments) raises a syntax error. batch mode rebuilds the table instead on
    # backends that need it, and is a no-op wrapper (plain ALTER) on Postgres.
    with op.batch_alter_table('interview_evaluations') as batch_op:
        batch_op.drop_column('created_at')
    with op.batch_alter_table('assessment_results') as batch_op:
        batch_op.drop_column('created_at')
