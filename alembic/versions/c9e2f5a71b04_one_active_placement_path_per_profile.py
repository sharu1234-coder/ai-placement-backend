"""one active placement path per profile

Phase 26. PlacementPath has always documented its own invariant -- "the active one is always the
single status=active row" -- and nothing enforced it. POST /placement-path/generate reads the
current active path, supersedes it, and inserts a new active one, with the ~163-second AI call
(the longest in the app) sitting in the middle of that sequence and outside any transaction.

Two requests are only the same *request* if they carry the same Idempotency-Key. Two that do not
were each entitled to proceed, and the real client produces exactly that pair: onboarding
generates the first path with its own key, and if that request is lost in transport the Placement
Path screen finds no current path and its Generate button sends a second one. Both read "no other
active path", both superseded the same old row, and both inserted an active one.

The result was not only two wasted generations. `get_current_placement_path` selected with an
unordered `.first()`, so with two active rows the *database* decided which roadmap the student
opened, and could decide differently on the next request.

This revision adds uq_placement_path_one_active_per_profile: UNIQUE (profile_id) WHERE
status = 'active'. A partial index, not UNIQUE(profile_id) -- superseded paths are history that a
profile accumulates for good, and the model deliberately never deletes them, so the column itself
cannot be unique. SQLite and PostgreSQL both support the predicate; it is written for each rather
than approximated by something portable that would forbid the history.

Duplicates already stored are resolved first, because the index cannot be created while they
exist. **Nothing is deleted.** Extra active rows are marked `superseded`, which is this model's
own word for a path that is no longer current, and every one of their placement_path_topics rows
-- carrying the student's topic status, completed_via and practice_blueprint_id -- is left exactly
as it is. They remain readable through GET /placement-path/history, which already lists active and
superseded paths together.

Which row stays active: the highest id for the profile, i.e. the most recently generated. That is
the one the student last asked for, and because regeneration carries progress forward by
(track_key, topic_key), it already contains the carried-forward progress of whatever it
superseded. In the race this closes, both rows were generated from the same predecessor within
seconds of each other, so they carry identical progress and keeping the newer loses nothing.

Measured before writing this: the developer database holds four profiles with a placement path
(one of them with a superseded one as well) and **no profile with more than one active path**, and
only 'active' and 'superseded' appear in the status column. So on this machine the resolution step
has nothing to do, and the migration test seeds duplicates on a throwaway database rather than
trusting it to be a no-op.

On PostgreSQL the table is locked against writes between the resolution and the index, so a new
duplicate cannot land in between. On SQLite the migration's first write already holds the whole
database. No table rebuild is needed on either engine: CREATE UNIQUE INDEX does not require one,
so placement_path_topics' foreign key to placement_paths.id is never dropped and recreated.

The downgrade drops the index. Rows already resolved stay resolved: re-activating a path this
migration superseded would reintroduce the duplicate, and choosing which of two to resurrect is
not a decision a downgrade can make.

Revision ID: c9e2f5a71b04
Revises: b1c47e0a52d9
Create Date: 2026-10-04
"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c9e2f5a71b04"
down_revision: Union[str, None] = "b1c47e0a52d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "placement_paths"
_INDEX = "uq_placement_path_one_active_per_profile"
_PREDICATE = "status = 'active'"

logger = logging.getLogger("alembic.runtime.migration")

# The columns this revision reads or writes, as they are at this revision -- never the live model,
# which will keep changing after it.
_paths = sa.table(
    _TABLE,
    sa.column("id", sa.Integer),
    sa.column("profile_id", sa.Integer),
    sa.column("status", sa.String),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"LOCK TABLE {_TABLE} IN SHARE ROW EXCLUSIVE MODE")
    _supersede_extra_active_paths(bind)
    op.create_index(
        _INDEX,
        _TABLE,
        ["profile_id"],
        unique=True,
        sqlite_where=sa.text(_PREDICATE),
        postgresql_where=sa.text(_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)


def _supersede_extra_active_paths(bind) -> None:
    """Leave the newest active path active for each profile and supersede the rest.

    One statement would do it on either engine, but the loop reports what it decided per profile,
    which is the only record a deployment will have of which roadmap it kept.
    """
    groups = bind.execute(
        sa.select(_paths.c.profile_id, sa.func.max(_paths.c.id))
        .where(_paths.c.status == "active")
        .group_by(_paths.c.profile_id)
        .having(sa.func.count() > 1)
        .order_by(_paths.c.profile_id)
    ).all()
    for profile_id, canonical_id in groups:
        superseded = bind.execute(
            _paths.update()
            .where(
                _paths.c.profile_id == profile_id,
                _paths.c.status == "active",
                _paths.c.id != canonical_id,
            )
            .values(status="superseded")
        ).rowcount
        logger.info(
            "Superseded %d extra active placement path(s) for profile %s; kept path %s active. "
            "No rows and no topic progress were deleted.",
            superseded, profile_id, canonical_id,
        )
