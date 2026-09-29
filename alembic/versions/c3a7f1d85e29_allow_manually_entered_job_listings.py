"""allow manually entered job listings

Phase 4. Until now a JobListing could only be created by provider ingestion, so the whole
outcome loop -- listing -> saved job -> career goal -> applied -> offer -- was gated on a
paid, shared-quota external search. This migration is the smallest schema change that lets a
student enter a job they found anywhere else.

Two widening changes, both on job_listings. Neither moves a value, neither drops anything, and
every existing row remains exactly as valid afterwards as it was before.

1. `url` becomes nullable.

   A provider advert always has a link -- one without it is dropped at ingestion, because
   there is nothing to apply to. A job found through campus placement, a referral, WhatsApp or
   an email genuinely has no URL. The alternatives were to refuse those jobs, which is most of
   the reason this phase exists, or to store a synthesized link, which the model has forbidden
   since it was written ("never a URL this app synthesizes") and which the client would then
   try to open. NULL is the honest record of "there is no page to send them to".

   Existing rows are unaffected: widening NOT NULL to NULL cannot invalidate a stored value,
   and no row is rewritten.

2. `created_by_profile_id` is added, nullable, indexed, referencing student_profiles.

   A provider listing is public content that many students may read; a manual one is the
   student's own text, including a job description they pasted. Without ownership on the row
   there is nothing to enforce that asymmetry against, and `GET /job-hunt/jobs/{id}` would
   hand one student's typed-in job to any other who guessed its id.

   NULL means "shared provider content", which is precisely what every pre-existing row is, so
   the backfill is the column default and there is nothing to migrate.

   It is a real foreign key rather than a bare integer on purpose: it makes the row
   profile-owned in SQLAlchemy's own metadata, which is what
   tests/unit/test_account_service.py::test_every_profile_owned_model_is_covered_by_account_deletion
   reads. Adding this column without handling account deletion now fails that test rather than
   leaking a deleted student's text.

batch_alter_table because SQLite (local development and the whole test suite) cannot ALTER a
column in place and cannot attach a named foreign key after the fact; batch mode rebuilds the
table and applies both changes at once. On PostgreSQL the same calls emit ordinary ALTERs.

The rebuild is the reason this migration is executed against seeded data in
tests/unit/test_manual_job_listing_migration.py rather than only read: job_listings is
referenced by saved_jobs, job_listing_matches and job_alert_deliveries, and a table rebuild
that quietly broke one of those references would be invisible to inspection.

Revision ID: c3a7f1d85e29
Revises: b4e17c9d05af
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3a7f1d85e29"
down_revision: Union[str, None] = "b4e17c9d05af"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("job_listings", schema=None) as batch_op:
        batch_op.alter_column(
            "url",
            existing_type=sa.String(length=1000),
            nullable=True,
        )
        batch_op.add_column(sa.Column("created_by_profile_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_job_listings_created_by_profile_id",
            "student_profiles",
            ["created_by_profile_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_job_listings_created_by_profile_id", ["created_by_profile_id"], unique=False
        )


def downgrade() -> None:
    # Not symmetrical, and deliberately so. Dropping the column is lossless for provider rows,
    # which never had one. Restoring NOT NULL on `url` would fail outright if any manual
    # listing without a link exists -- which is the correct outcome: those rows cannot be
    # represented in the older schema, and silently deleting a student's tracked jobs to make
    # a downgrade succeed would be far worse than a downgrade that refuses.
    with op.batch_alter_table("job_listings", schema=None) as batch_op:
        batch_op.drop_index("ix_job_listings_created_by_profile_id")
        batch_op.drop_constraint("fk_job_listings_created_by_profile_id", type_="foreignkey")
        batch_op.drop_column("created_by_profile_id")
        batch_op.alter_column(
            "url",
            existing_type=sa.String(length=1000),
            nullable=False,
        )
