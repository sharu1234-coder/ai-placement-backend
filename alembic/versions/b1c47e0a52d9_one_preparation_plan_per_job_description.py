"""one preparation plan per job description

Phase 25. POST /jobs/{jd_id}/preparation-plan has always meant "make sure this job description
has a plan", not "make me another one": there is no regenerate route, no delete-plan route, and
both Android callers either read before they write or run once against a job description they
have only just created. Both server read paths -- get_preparation_plan and the saved-jobs list's
latest-plan-per-jd map -- take the newest row for a (profile_id, jd_id) and discard the rest.

Nothing in the database enforced it, and the route could not. Two requests are only the same
*request* if they carry the same Idempotency-Key, and two that do not were each entitled to
proceed: the analyse flow posting (company, title) and the result screen's 404 recovery posting
(null, null), two devices, or an attempt that timed out in transport and was reopened from the
student's saved list. Both found no plan, both ran the model, and both inserted. The student paid
for two expensive generations and the table kept a row they could never see.

This revision adds uq_job_preparation_plan_per_jd, UNIQUE (profile_id, jd_id). The constraint
cannot be created while duplicates exist, so any already stored are resolved first.

Which row survives: the highest id for the (profile_id, jd_id) pair. That is deliberately the
row every read path already returns, which is the whole point -- applying this migration cannot
change what any student currently sees in the app. The rows it removes are ones no read path
could reach.

What is lost. A non-canonical plan is a complete AI snapshot (summary, gaps, guidance, interview
questions), and it is deleted rather than merged. Merging would be wrong here even if it were
possible: unlike a career path, a plan holds no student progress -- no statuses, no completions,
nothing the student entered -- only generated prose about one job description, and the canonical
row has its own, newer version of every section. There is nothing in the older row that the newer
one does not already answer.

Nothing is orphaned. No table carries a foreign key to job_preparation_plans; the only other
reference anywhere is operation_claims.result_id, and the preparation-plan replay path re-reads
the authoritative plan for the job description rather than fetching that id, so a claim left
pointing at a removed row still resolves to the plan that exists.

On PostgreSQL the table is locked against writes for the window between the delete and the
constraint, so a new duplicate cannot land in between. On SQLite the migration's first write
already holds the whole database.

batch_alter_table because SQLite can only add a table-level constraint by rebuilding the table;
on PostgreSQL it is a plain ALTER TABLE ... ADD CONSTRAINT. That rebuild is why
tests/unit/test_preparation_plan_uniqueness_migration.py runs this against seeded duplicates on
both engines rather than trusting it to be a no-op.

The downgrade drops the constraint. Rows already de-duplicated stay de-duplicated: the deletes
cannot be undone, and inventing replacement plans would be worse than leaving them gone.

Revision ID: b1c47e0a52d9
Revises: 7f3c21d9ab48
Create Date: 2026-10-04
"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b1c47e0a52d9"
down_revision: Union[str, None] = "7f3c21d9ab48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "job_preparation_plans"
_CONSTRAINT = "uq_job_preparation_plan_per_jd"

logger = logging.getLogger("alembic.runtime.migration")

# The columns this revision reads, as they are at this revision -- never the live model, which
# will keep changing after it.
_plans = sa.table(
    _TABLE,
    sa.column("id", sa.Integer),
    sa.column("jd_id", sa.Integer),
    sa.column("profile_id", sa.Integer),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"LOCK TABLE {_TABLE} IN SHARE ROW EXCLUSIVE MODE")
    _remove_shadowed_plans(bind)
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.create_unique_constraint(_CONSTRAINT, ["profile_id", "jd_id"])


def downgrade() -> None:
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="unique")


def _remove_shadowed_plans(bind) -> None:
    """Delete every plan that a newer plan for the same (profile, job description) already hides.

    One statement would do it on either engine, but the loop reports what it removed per pair,
    which is the only record a deployment will have of what this migration decided.
    """
    groups = bind.execute(
        sa.select(_plans.c.profile_id, _plans.c.jd_id, sa.func.max(_plans.c.id))
        .group_by(_plans.c.profile_id, _plans.c.jd_id)
        .having(sa.func.count() > 1)
        .order_by(_plans.c.profile_id, _plans.c.jd_id)
    ).all()
    for profile_id, jd_id, canonical_id in groups:
        removed = bind.execute(
            _plans.delete().where(
                _plans.c.profile_id == profile_id,
                _plans.c.jd_id == jd_id,
                _plans.c.id != canonical_id,
            )
        ).rowcount
        logger.info(
            "Removed %d shadowed preparation plan(s) for profile %s, jd %s; kept plan %s",
            removed, profile_id, jd_id, canonical_id,
        )
