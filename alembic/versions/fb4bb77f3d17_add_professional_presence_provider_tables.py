"""add professional presence provider tables

Revision ID: fb4bb77f3d17
Revises: e7c3a95d1b84
Create Date: 2026-09-13 14:24:13.968948

Four tables behind the GitHub/LinkedIn connection feature. Nothing in this migration touches
an existing table, and nothing backfills: every table starts empty and the feature is inert
until a student connects a provider, so applying this on a live database changes no behaviour.

The two partial unique indexes on provider_connections are the only part that is not routine,
and they are the reason this migration is worth reading rather than skimming:

  uq_provider_connection_active_per_provider  (profile_id, provider) WHERE revoked_at IS NULL
  uq_provider_connection_active_identity      (provider, provider_user_id) WHERE revoked_at IS NULL

They are partial because disconnecting must genuinely release both slots. A plain unique
constraint would let a student connect GitHub exactly once ever -- disconnect, and they could
never reconnect, because the revoked row would still occupy the pair. Making the predicate
`revoked_at IS NULL` means history accumulates freely while only one row is ever live.

The second index is the security-relevant one: without it, two accounts could each hold a live
connection to the same GitHub identity, and one student's public repositories would count as
another student's career evidence.

Both carry `sqlite_where` alongside `postgresql_where`. SQLite has supported partial indexes
since 3.8.0, and without the dialect-specific clause SQLAlchemy emits a *full* unique index
there -- so the development database would reject a legitimate reconnect-after-revoke that
production accepts. Same predicate, both dialects, so the two databases agree. PostgreSQL's
behaviour is still asserted directly in tests/unit/test_provider_connection_constraints.py;
this is not a substitute for testing the database that actually runs in production.

Downgrade drops all four in reverse dependency order (children before provider_connections)
and is exercised base -> head -> base -> head, not merely written.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'fb4bb77f3d17'
down_revision: Union[str, None] = 'e7c3a95d1b84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'provider_connections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('provider_user_id', sa.String(length=64), nullable=False),
        sa.Column('provider_username', sa.String(length=120), nullable=True),
        sa.Column('access_token_encrypted', sa.Text(), nullable=True),
        sa.Column('refresh_token_encrypted', sa.Text(), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('granted_scopes', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('sync_status', sa.String(length=20), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error_kind', sa.String(length=40), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['student_profiles.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_provider_connections_profile_id'), 'provider_connections', ['profile_id'], unique=False
    )
    op.create_index(
        'uq_provider_connection_active_identity',
        'provider_connections',
        ['provider', 'provider_user_id'],
        unique=True,
        postgresql_where=sa.text('revoked_at IS NULL'),
        sqlite_where=sa.text('revoked_at IS NULL'),
    )
    op.create_index(
        'uq_provider_connection_active_per_provider',
        'provider_connections',
        ['profile_id', 'provider'],
        unique=True,
        postgresql_where=sa.text('revoked_at IS NULL'),
        sqlite_where=sa.text('revoked_at IS NULL'),
    )

    op.create_table(
        'provider_oauth_states',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('state_hash', sa.String(length=64), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('code_verifier_encrypted', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['student_profiles.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('state_hash'),
    )
    op.create_index(
        op.f('ix_provider_oauth_states_expires_at'), 'provider_oauth_states', ['expires_at'], unique=False
    )
    op.create_index(
        op.f('ix_provider_oauth_states_profile_id'), 'provider_oauth_states', ['profile_id'], unique=False
    )

    op.create_table(
        'external_evidence_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('skill', sa.String(length=120), nullable=False),
        sa.Column('signal_kind', sa.String(length=40), nullable=False),
        sa.Column('source_ref', sa.String(length=255), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['provider_connections.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'connection_id', 'skill', 'signal_kind', 'source_ref', name='uq_external_evidence_item_signal'
        ),
    )
    op.create_index(
        op.f('ix_external_evidence_items_skill'), 'external_evidence_items', ['skill'], unique=False
    )

    op.create_table(
        'external_profile_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['provider_connections.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('connection_id', 'sequence', name='uq_external_profile_snapshot_sequence'),
    )
    op.create_index(
        op.f('ix_external_profile_snapshots_connection_id'),
        'external_profile_snapshots',
        ['connection_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_external_profile_snapshots_fingerprint'),
        'external_profile_snapshots',
        ['fingerprint'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_external_profile_snapshots_fingerprint'), table_name='external_profile_snapshots')
    op.drop_index(op.f('ix_external_profile_snapshots_connection_id'), table_name='external_profile_snapshots')
    op.drop_table('external_profile_snapshots')
    op.drop_index(op.f('ix_external_evidence_items_skill'), table_name='external_evidence_items')
    op.drop_table('external_evidence_items')
    op.drop_index(op.f('ix_provider_oauth_states_profile_id'), table_name='provider_oauth_states')
    op.drop_index(op.f('ix_provider_oauth_states_expires_at'), table_name='provider_oauth_states')
    op.drop_table('provider_oauth_states')
    op.drop_index(
        'uq_provider_connection_active_per_provider',
        table_name='provider_connections',
        postgresql_where=sa.text('revoked_at IS NULL'),
        sqlite_where=sa.text('revoked_at IS NULL'),
    )
    op.drop_index(
        'uq_provider_connection_active_identity',
        table_name='provider_connections',
        postgresql_where=sa.text('revoked_at IS NULL'),
        sqlite_where=sa.text('revoked_at IS NULL'),
    )
    op.drop_index(op.f('ix_provider_connections_profile_id'), table_name='provider_connections')
    op.drop_table('provider_connections')
