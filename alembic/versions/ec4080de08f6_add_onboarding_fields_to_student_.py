"""add onboarding fields to student profiles

Revision ID: ec4080de08f6
Revises: a2a45e576092
Create Date: 2026-08-25 23:05:50.350751

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ec4080de08f6'
down_revision: Union[str, None] = 'a2a45e576092'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch mode: SQLite (local dev) can't ALTER a constraint onto an existing table
    # directly -- batch mode does a copy-and-move there, and executes the plain
    # statements unchanged on dialects (e.g. Postgres/prod) that support ALTER directly.
    with op.batch_alter_table('student_profiles') as batch_op:
        batch_op.add_column(sa.Column('degree', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('branch', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('experience_level', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('preparation_goals', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('secondary_target_role_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('custom_target_role', sa.String(length=150), nullable=True))
        batch_op.add_column(sa.Column('onboarding_completed_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            'fk_student_profiles_secondary_target_role_id_roles',
            'roles', ['secondary_target_role_id'], ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('student_profiles') as batch_op:
        batch_op.drop_constraint('fk_student_profiles_secondary_target_role_id_roles', type_='foreignkey')
        batch_op.drop_column('onboarding_completed_at')
        batch_op.drop_column('custom_target_role')
        batch_op.drop_column('secondary_target_role_id')
        batch_op.drop_column('preparation_goals')
        batch_op.drop_column('experience_level')
        batch_op.drop_column('branch')
        batch_op.drop_column('degree')
