"""add job alerts, tracker provenance and saved-search run metadata

Revision ID: a890ff066a0a
Revises: 9b4d3dfbe27f
Create Date: 2026-09-08

Three things, and two of them needed hand-correction after autogeneration.

`saved_jobs.source` is NOT NULL, and every existing row predates it. Autogenerate emitted the
column without a default, which fails on any database that already has saved jobs in it -- the
exact case this migration will meet in production and never meets on a fresh test database.
It carries server_default='manual', which is also the honest value: a row saved before this
column existed came from somewhere we did not record, and "manual" is what we can defend.

The foreign key and its removal are wrapped in batch_alter_table. SQLite cannot ALTER a table
to add or drop a constraint, so the plain op.create_foreign_key autogenerate produced would
work on PostgreSQL and fail on the SQLite this project's tests and local development run on.
Autogenerate also emitted `op.drop_constraint(None, ...)` in the downgrade, which raises
before it does anything at all; the constraint is named explicitly here so the downgrade
actually reverses the upgrade.

Downgrade order is load-bearing: job_alert_deliveries references job_alerts, so it goes first.
job_listings is untouched throughout -- it is shared provider content, not per-student data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a890ff066a0a"
down_revision: Union[str, None] = "9b4d3dfbe27f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SAVED_JOB_SOURCE_FK = "fk_saved_jobs_source_saved_search_id"


def upgrade() -> None:
    op.create_table(
        "job_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("saved_search_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("hour_local", sa.Integer(), nullable=False),
        sa.Column("quiet_hours_start", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_end", sa.Integer(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_status", sa.String(length=32), nullable=True),
        sa.Column("last_new_job_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["student_profiles.id"]),
        sa.ForeignKeyConstraint(["saved_search_id"], ["job_saved_searches.id"]),
        sa.PrimaryKeyConstraint("id"),
        # One alert per saved search: a second would simply double every notification.
        sa.UniqueConstraint(
            "profile_id", "saved_search_id", name="uq_job_alert_per_saved_search"
        ),
    )
    op.create_index(op.f("ix_job_alerts_profile_id"), "job_alerts", ["profile_id"], unique=False)
    op.create_index(
        op.f("ix_job_alerts_saved_search_id"), "job_alerts", ["saved_search_id"], unique=False
    )

    op.create_table(
        "job_alert_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["job_alerts.id"]),
        sa.ForeignKeyConstraint(["listing_id"], ["job_listings.id"]),
        sa.ForeignKeyConstraint(["profile_id"], ["student_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        # The whole duplicate-suppression mechanism. Enforced here rather than in service code
        # because a check-then-insert in Python loses the race the moment two runs overlap.
        sa.UniqueConstraint("alert_id", "listing_id", name="uq_alert_delivery_per_listing"),
    )
    op.create_index(
        op.f("ix_job_alert_deliveries_alert_id"), "job_alert_deliveries", ["alert_id"], unique=False
    )
    op.create_index(
        op.f("ix_job_alert_deliveries_listing_id"),
        "job_alert_deliveries",
        ["listing_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_alert_deliveries_profile_id"),
        "job_alert_deliveries",
        ["profile_id"],
        unique=False,
    )

    op.add_column("job_saved_searches", sa.Column("last_success_at", sa.DateTime(), nullable=True))
    op.add_column("job_saved_searches", sa.Column("last_error_at", sa.DateTime(), nullable=True))

    # server_default so existing rows get a value; see the module docstring.
    op.add_column(
        "saved_jobs",
        sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
    )
    op.add_column("saved_jobs", sa.Column("source_saved_search_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_saved_jobs_source_saved_search_id"),
        "saved_jobs",
        ["source_saved_search_id"],
        unique=False,
    )
    # batch mode so this also works on SQLite, which cannot ALTER in a constraint.
    with op.batch_alter_table("saved_jobs") as batch_op:
        batch_op.create_foreign_key(
            _SAVED_JOB_SOURCE_FK,
            "job_saved_searches",
            ["source_saved_search_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("saved_jobs") as batch_op:
        batch_op.drop_constraint(_SAVED_JOB_SOURCE_FK, type_="foreignkey")
    op.drop_index(op.f("ix_saved_jobs_source_saved_search_id"), table_name="saved_jobs")
    op.drop_column("saved_jobs", "source_saved_search_id")
    op.drop_column("saved_jobs", "source")

    op.drop_column("job_saved_searches", "last_error_at")
    op.drop_column("job_saved_searches", "last_success_at")

    # Deliveries reference alerts, so they go first.
    op.drop_index(op.f("ix_job_alert_deliveries_profile_id"), table_name="job_alert_deliveries")
    op.drop_index(op.f("ix_job_alert_deliveries_listing_id"), table_name="job_alert_deliveries")
    op.drop_index(op.f("ix_job_alert_deliveries_alert_id"), table_name="job_alert_deliveries")
    op.drop_table("job_alert_deliveries")

    op.drop_index(op.f("ix_job_alerts_saved_search_id"), table_name="job_alerts")
    op.drop_index(op.f("ix_job_alerts_profile_id"), table_name="job_alerts")
    op.drop_table("job_alerts")
