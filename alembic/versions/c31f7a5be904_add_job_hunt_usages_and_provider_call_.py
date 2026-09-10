"""add job_hunt_usages and provider_call_budgets

The two limits Job Hunt now enforces, and they are deliberately two tables rather than one.

`job_hunt_usages` is the per-student entitlement ledger: one row per Job Hunt operation, with
the single free search enforced by a unique constraint rather than by application code. The
constraint on (profile_id, free_use_slot) is the whole mechanism -- NULLs are distinct in a
unique constraint in both PostgreSQL and SQLite, so a profile may hold any number of NULL rows
and at most one row where the slot is 1. See app/models/job_hunt_usage.py.

`provider_call_budgets` is the per-deployment provider ceiling: Jobvetta grants 50 requests per
API key per UTC day, which belongs to the key rather than to anyone's subscription. Keeping it
out of the ledger keeps "you have used your free search" and "the day's provider budget is
gone" from ever being able to answer for each other.

Neither table has an ondelete rule, matching every other foreign key in this schema (NO
ACTION). Account deletion removes job_hunt_usages explicitly in
app/services/account_service.py, before the student_profiles row it points at.

Revision ID: c31f7a5be904
Revises: a890ff066a0a
Create Date: 2026-09-10

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c31f7a5be904'
down_revision: Union[str, None] = 'a890ff066a0a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_hunt_usages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('usage_type', sa.String(length=30), nullable=False),
        sa.Column('idempotency_key', sa.String(length=64), nullable=False),
        sa.Column('request_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('free_use_slot', sa.Integer(), nullable=True),
        sa.Column('was_premium', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['student_profiles.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('profile_id', 'idempotency_key', name='uq_job_hunt_usage_idempotency'),
        sa.UniqueConstraint('profile_id', 'free_use_slot', name='uq_job_hunt_usage_free_slot'),
    )
    op.create_index(op.f('ix_job_hunt_usages_profile_id'), 'job_hunt_usages', ['profile_id'])
    op.create_index(op.f('ix_job_hunt_usages_status'), 'job_hunt_usages', ['status'])
    # Serves the two hot reads: "does this profile hold the free slot" and "does this profile
    # have a stale claim to reclaim". Both filter on profile_id and status together.
    op.create_index(
        'ix_job_hunt_usages_profile_status', 'job_hunt_usages', ['profile_id', 'status']
    )

    op.create_table(
        'provider_call_budgets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('usage_date', sa.Date(), nullable=False),
        sa.Column('call_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'usage_date', name='uq_provider_call_budget_day'),
    )


def downgrade() -> None:
    op.drop_table('provider_call_budgets')
    op.drop_index('ix_job_hunt_usages_profile_status', table_name='job_hunt_usages')
    op.drop_index(op.f('ix_job_hunt_usages_status'), table_name='job_hunt_usages')
    op.drop_index(op.f('ix_job_hunt_usages_profile_id'), table_name='job_hunt_usages')
    op.drop_table('job_hunt_usages')
