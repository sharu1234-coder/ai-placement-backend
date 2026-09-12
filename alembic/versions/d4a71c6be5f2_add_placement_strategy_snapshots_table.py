"""add placement_strategy_snapshots table

Revision ID: d4a71c6be5f2
Revises: c31f7a5be904
Create Date: 2026-09-11 10:12:03.441907

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a71c6be5f2'
down_revision: Union[str, None] = 'c31f7a5be904'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('placement_strategy_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('profile_id', sa.Integer(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('strategy_version', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=30), nullable=False),
    sa.Column('primary_bottleneck_id', sa.String(length=200), nullable=True),
    sa.Column('primary_category', sa.String(length=40), nullable=True),
    sa.Column('primary_subject', sa.String(length=200), nullable=True),
    sa.Column('decision_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('decision', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['profile_id'], ['student_profiles.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('profile_id', 'sequence', name='uq_placement_strategy_snapshot_sequence')
    )
    op.create_index(op.f('ix_placement_strategy_snapshots_profile_id'), 'placement_strategy_snapshots', ['profile_id'], unique=False)
    op.create_index(op.f('ix_placement_strategy_snapshots_decision_fingerprint'), 'placement_strategy_snapshots', ['decision_fingerprint'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_placement_strategy_snapshots_decision_fingerprint'), table_name='placement_strategy_snapshots')
    op.drop_index(op.f('ix_placement_strategy_snapshots_profile_id'), table_name='placement_strategy_snapshots')
    op.drop_table('placement_strategy_snapshots')
