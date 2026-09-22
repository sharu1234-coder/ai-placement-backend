"""add career_goals table

Revision ID: 7c41a0d9e2b5
Revises: 0d6496be2213
Create Date: 2026-09-22 10:14:03.551207

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c41a0d9e2b5'
down_revision: Union[str, None] = '0d6496be2213'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('career_goals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('profile_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('company', sa.String(length=200), nullable=False),
    sa.Column('location', sa.String(length=200), nullable=True),
    sa.Column('job_description', sa.Text(), nullable=False),
    # Nullable: a goal exists before it has ever been analyzed, and must survive an analysis
    # that fails. ondelete is deliberately NOT set -- career_goal_service deletes the parse it
    # owns explicitly, so a silent cascade would only hide a bug in that path.
    sa.Column('jd_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['jd_id'], ['job_descriptions.id'], ),
    sa.ForeignKeyConstraint(['profile_id'], ['student_profiles.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_career_goals_jd_id'), 'career_goals', ['jd_id'], unique=False)
    op.create_index(op.f('ix_career_goals_profile_id'), 'career_goals', ['profile_id'], unique=False)
    op.create_index(op.f('ix_career_goals_status'), 'career_goals', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_career_goals_status'), table_name='career_goals')
    op.drop_index(op.f('ix_career_goals_profile_id'), table_name='career_goals')
    op.drop_index(op.f('ix_career_goals_jd_id'), table_name='career_goals')
    op.drop_table('career_goals')
