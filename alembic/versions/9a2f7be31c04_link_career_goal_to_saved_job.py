"""link career_goal to saved_job

Revision ID: 9a2f7be31c04
Revises: 7c41a0d9e2b5
Create Date: 2026-09-24 09:41:18.204533

Adds the single pointer that closes the outcome loop: which real-world application a career
goal is being pursued through. One nullable column, no data migration -- every existing goal
and every existing saved job keeps working untouched, with the link simply NULL.

batch_alter_table rather than three bare ALTERs because SQLite (used by the whole test suite
and by local development) cannot ADD COLUMN with a UNIQUE constraint or attach a named foreign
key afterwards; batch mode rebuilds the table and applies all three at once. On PostgreSQL the
same calls emit ordinary ALTER statements.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a2f7be31c04'
down_revision: Union[str, None] = '7c41a0d9e2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('career_goals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('saved_job_id', sa.Integer(), nullable=True))
        # UNIQUE is the product rule (one application belongs to at most one goal) AND the
        # concurrency arbiter for two requests claiming the same job at once. Multiple NULLs
        # are permitted by both SQLite and PostgreSQL, so unlinked goals are unaffected.
        batch_op.create_unique_constraint('uq_career_goals_saved_job_id', ['saved_job_id'])
        # SET NULL, not RESTRICT or CASCADE. job_hunt deletes a saved job outright and account
        # deletion removes saved_jobs before career_goals, so a plain reference would raise;
        # CASCADE would delete the student's goal because they tidied up their job tracker,
        # which destroys an intention they still hold.
        batch_op.create_foreign_key(
            'fk_career_goals_saved_job_id',
            'saved_jobs',
            ['saved_job_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    # Reversible and lossless in the only sense that matters: dropping the column discards the
    # links, but neither career_goals nor saved_jobs loses a row, so downgrading returns the
    # database to exactly the shape 7c41a0d9e2b5 left it in.
    with op.batch_alter_table('career_goals', schema=None) as batch_op:
        batch_op.drop_constraint('fk_career_goals_saved_job_id', type_='foreignkey')
        batch_op.drop_constraint('uq_career_goals_saved_job_id', type_='unique')
        batch_op.drop_column('saved_job_id')
