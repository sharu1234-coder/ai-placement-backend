"""add career_strategy_targets table

Revision ID: e7c3a95d1b84
Revises: d4a71c6be5f2
Create Date: 2026-09-11 11:48:22.118904

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7c3a95d1b84'
down_revision: Union[str, None] = 'd4a71c6be5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('career_strategy_targets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('profile_id', sa.Integer(), nullable=False),
    sa.Column('role_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['profile_id'], ['student_profiles.id'], ),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('profile_id', 'role_id', name='uq_career_strategy_target_per_role')
    )
    op.create_index(op.f('ix_career_strategy_targets_profile_id'), 'career_strategy_targets', ['profile_id'], unique=False)
    op.create_index(op.f('ix_career_strategy_targets_role_id'), 'career_strategy_targets', ['role_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_career_strategy_targets_role_id'), table_name='career_strategy_targets')
    op.drop_index(op.f('ix_career_strategy_targets_profile_id'), table_name='career_strategy_targets')
    op.drop_table('career_strategy_targets')
