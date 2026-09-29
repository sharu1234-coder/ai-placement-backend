"""enforce one career path per role family

F12-03. A CareerPath has always been documented as one row per (profile_id, role_family_key):
re-selecting a role finds and refreshes the same row, carrying the student's progress forward.
Nothing in the database enforced it. select_role() looked the row up and inserted one when it
found none, so two selections of the same role that overlapped -- a retry sent while the first
was still generating, a second device -- both found none and both inserted. Measured against the
unchanged backend: on PostgreSQL every overlapping request created its own row, all of them
marked primary; on SQLite the second request either failed with "database is locked" or created
the duplicate. The student then saw the role twice among their saved paths, and progress made on
one copy looked lost as soon as a later selection picked the other.

This revision adds uq_career_path_per_role_family, UNIQUE (profile_id, role_family_key). Any
duplicates already stored are merged first: the constraint cannot be created while they exist,
and "keep one, delete the rest" would delete progress.

Which row survives -- the canonical row -- in order of preference:
  1. the one marked primary: the path the student is shown now (GET /career-path/current);
  2. the one with the most completed work (completed skill items plus completed projects);
  3. the most recently updated;
  4. the lowest id, i.e. the first created.
The same data always merges the same way.

Every row that references career_paths.id belongs to one of three tables -- phases, skill items,
projects -- and each duplicate's rows are handled as follows.
  * A skill item or project whose key the canonical row also has is merged into it. The canonical
    item takes the further-along status (completed > in_progress > not_started), with the
    completed_via that came with it, and a practice blueprint link if it has none of its own.
    The duplicate's item is then removed: what it recorded now lives on the canonical item.
  * One whose key the canonical row lacks -- a custom role generated twice with different skills,
    a skill added from a job description -- is moved to the canonical row intact, after that
    phase's existing items.
  * A phase is removed if the canonical row has one with the same key, moved otherwise.
The duplicate row itself goes last. By rule 1 the canonical row is primary whenever any copy
was; it also takes the latest updated_at among the copies, so the saved-paths list, ordered by
it, still reflects the student's latest activity on this role. Where the unfixed PostgreSQL path
left every copy marked primary, only the canonical one survives, so the profile is left with a
single primary path for this role.

Not addressed here, deliberately: the same unfixed path could also mark two paths for *different*
roles primary at once, which is not a duplicate row and not this invariant. Choosing which of two
genuinely different target roles is the current one is a product decision, not a merge, so the
rows are left as they are -- and select_role() now clears the flag on every one of a profile's
paths before setting it, so the next role selection resolves it.

Stated limits. A duplicate's own generated text (its overview, a custom role's cached
definition) is not kept: it is AI content, not the student's work, and the canonical row has its
own. Items moved across under keys the canonical definition does not produce are kept by this
migration, but the next rebuild of the path (re-selecting or refreshing the role) keeps only the
keys its definition produces -- the existing carry-forward-by-key behaviour, unchanged here, and
what already happens to skills added from a job description.

On PostgreSQL the four tables are locked against writes until the constraint exists, so neither
a new duplicate nor a progress update on a row being merged away can land in between. On SQLite
the migration's first write already holds the whole database.

batch_alter_table because SQLite can only add the constraint by rebuilding the table; on
PostgreSQL it is a plain ALTER TABLE ... ADD CONSTRAINT. The rebuild is why
tests/unit/test_career_path_uniqueness_migration.py runs this against seeded rows, children
included, on both engines. The downgrade drops the constraint; merged rows stay merged.

Revision ID: 5b8e2f7c1a94
Revises: c3a7f1d85e29
Create Date: 2026-09-26
"""
import logging
from typing import Dict, List, Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "5b8e2f7c1a94"
down_revision: Union[str, None] = "c3a7f1d85e29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "uq_career_path_per_role_family"

# When two copies of one skill or project disagree, the further-along status wins.
_PROGRESS = {"not_started": 0, "in_progress": 1, "completed": 2}

logger = logging.getLogger("alembic.runtime.migration")

# The columns this revision reads or writes, as they are at this revision -- never the live
# models, which will keep changing after it.
_paths = sa.table(
    "career_paths",
    sa.column("id", sa.Integer),
    sa.column("profile_id", sa.Integer),
    sa.column("role_family_key", sa.String),
    sa.column("is_primary", sa.Boolean),
    sa.column("updated_at", sa.DateTime),
)
_phases = sa.table(
    "career_path_phases",
    sa.column("id", sa.Integer),
    sa.column("career_path_id", sa.Integer),
    sa.column("phase_key", sa.String),
)
_skills = sa.table(
    "career_path_skill_items",
    sa.column("id", sa.Integer),
    sa.column("career_path_id", sa.Integer),
    sa.column("phase_key", sa.String),
    sa.column("skill_key", sa.String),
    sa.column("order_index", sa.Integer),
    sa.column("status", sa.String),
    sa.column("completed_via", sa.String),
    sa.column("practice_blueprint_id", sa.Integer),
)
_projects = sa.table(
    "career_path_projects",
    sa.column("id", sa.Integer),
    sa.column("career_path_id", sa.Integer),
    sa.column("project_key", sa.String),
    sa.column("order_index", sa.Integer),
    sa.column("status", sa.String),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE career_paths, career_path_phases, career_path_skill_items, career_path_projects"
            " IN SHARE ROW EXCLUSIVE MODE"
        )
    _merge_duplicates(bind)
    with op.batch_alter_table("career_paths", schema=None) as batch_op:
        batch_op.create_unique_constraint(_CONSTRAINT, ["profile_id", "role_family_key"])


def downgrade() -> None:
    with op.batch_alter_table("career_paths", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="unique")


def _merge_duplicates(bind) -> None:
    groups = bind.execute(
        sa.select(_paths.c.profile_id, _paths.c.role_family_key)
        .group_by(_paths.c.profile_id, _paths.c.role_family_key)
        .having(sa.func.count() > 1)
        .order_by(_paths.c.profile_id, _paths.c.role_family_key)
    ).all()
    for profile_id, family_key in groups:
        copies = bind.execute(
            sa.select(_paths.c.id, _paths.c.is_primary, _paths.c.updated_at).where(
                _paths.c.profile_id == profile_id, _paths.c.role_family_key == family_key
            )
        ).all()
        canonical, *duplicates = _ranked(bind, copies)
        for duplicate in duplicates:
            _merge_skill_items(bind, canonical.id, duplicate.id)
            _merge_projects(bind, canonical.id, duplicate.id)
            _merge_phases(bind, canonical.id, duplicate.id)
            bind.execute(_paths.delete().where(_paths.c.id == duplicate.id))
        bind.execute(
            _paths.update()
            .where(_paths.c.id == canonical.id)
            .values(updated_at=max(copy.updated_at for copy in copies))
        )
        logger.info(
            "Merged %d duplicate career path(s) for profile %s, role family %s into career path %s",
            len(duplicates), profile_id, family_key, canonical.id,
        )


def _ranked(bind, copies) -> List:
    """Canonical first: primary, then most completed work, then most recently updated, then
    lowest id. Each sort is stable, so sorting by the least significant key first leaves the
    most significant one in charge."""
    completed = {copy.id: _completed_work(bind, copy.id) for copy in copies}
    ranked = sorted(copies, key=lambda copy: copy.id)
    ranked.sort(key=lambda copy: copy.updated_at, reverse=True)
    ranked.sort(key=lambda copy: completed[copy.id], reverse=True)
    ranked.sort(key=lambda copy: bool(copy.is_primary), reverse=True)
    return ranked


def _completed_work(bind, path_id: int) -> int:
    skills = bind.execute(
        sa.select(sa.func.count())
        .select_from(_skills)
        .where(_skills.c.career_path_id == path_id, _skills.c.status == "completed")
    ).scalar_one()
    projects = bind.execute(
        sa.select(sa.func.count())
        .select_from(_projects)
        .where(_projects.c.career_path_id == path_id, _projects.c.status == "completed")
    ).scalar_one()
    return skills + projects


def _further_along(status: str, than: str) -> bool:
    return _PROGRESS.get(status, 0) > _PROGRESS.get(than, 0)


def _merge_skill_items(bind, canonical_id: int, duplicate_id: int) -> None:
    kept: Dict[str, List[dict]] = {}
    for item in bind.execute(sa.select(_skills).where(_skills.c.career_path_id == canonical_id)).all():
        kept.setdefault(item.skill_key, []).append(dict(item._mapping))
    next_order = {
        phase_key: top + 1
        for phase_key, top in bind.execute(
            sa.select(_skills.c.phase_key, sa.func.max(_skills.c.order_index))
            .where(_skills.c.career_path_id == canonical_id)
            .group_by(_skills.c.phase_key)
        )
    }
    moving = bind.execute(
        sa.select(_skills)
        .where(_skills.c.career_path_id == duplicate_id)
        .order_by(_skills.c.phase_key, _skills.c.order_index, _skills.c.id)
    ).all()
    for item in moving:
        targets = kept.get(item.skill_key)
        if not targets:
            order = next_order.get(item.phase_key, 0)
            next_order[item.phase_key] = order + 1
            bind.execute(
                _skills.update().where(_skills.c.id == item.id).values(career_path_id=canonical_id, order_index=order)
            )
            kept[item.skill_key] = [dict(item._mapping, career_path_id=canonical_id, order_index=order)]
            continue
        for target in targets:
            values = {}
            if _further_along(item.status, target["status"]):
                values.update(status=item.status, completed_via=item.completed_via)
            if target["practice_blueprint_id"] is None and item.practice_blueprint_id is not None:
                values["practice_blueprint_id"] = item.practice_blueprint_id
            if values:
                bind.execute(_skills.update().where(_skills.c.id == target["id"]).values(**values))
                target.update(values)
        bind.execute(_skills.delete().where(_skills.c.id == item.id))


def _merge_projects(bind, canonical_id: int, duplicate_id: int) -> None:
    kept: Dict[str, List[dict]] = {}
    for project in bind.execute(sa.select(_projects).where(_projects.c.career_path_id == canonical_id)).all():
        kept.setdefault(project.project_key, []).append(dict(project._mapping))
    top = bind.execute(
        sa.select(sa.func.max(_projects.c.order_index)).where(_projects.c.career_path_id == canonical_id)
    ).scalar_one()
    next_order = 0 if top is None else top + 1
    moving = bind.execute(
        sa.select(_projects)
        .where(_projects.c.career_path_id == duplicate_id)
        .order_by(_projects.c.order_index, _projects.c.id)
    ).all()
    for project in moving:
        targets = kept.get(project.project_key)
        if not targets:
            bind.execute(
                _projects.update()
                .where(_projects.c.id == project.id)
                .values(career_path_id=canonical_id, order_index=next_order)
            )
            kept[project.project_key] = [dict(project._mapping, career_path_id=canonical_id, order_index=next_order)]
            next_order += 1
            continue
        for target in targets:
            if _further_along(project.status, target["status"]):
                bind.execute(_projects.update().where(_projects.c.id == target["id"]).values(status=project.status))
                target["status"] = project.status
        bind.execute(_projects.delete().where(_projects.c.id == project.id))


def _merge_phases(bind, canonical_id: int, duplicate_id: int) -> None:
    present = {
        phase_key
        for (phase_key,) in bind.execute(
            sa.select(_phases.c.phase_key).where(_phases.c.career_path_id == canonical_id)
        )
    }
    for phase_id, phase_key in bind.execute(
        sa.select(_phases.c.id, _phases.c.phase_key).where(_phases.c.career_path_id == duplicate_id)
    ).all():
        if phase_key in present:
            bind.execute(_phases.delete().where(_phases.c.id == phase_id))
        else:
            bind.execute(_phases.update().where(_phases.c.id == phase_id).values(career_path_id=canonical_id))
            present.add(phase_key)
