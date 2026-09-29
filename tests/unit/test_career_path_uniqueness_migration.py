"""The Phase 15 migration, run against real duplicate rows rather than read.

Every other test in this suite builds its schema with create_all from the models, which would pass
identically if this migration were empty. Production is built by Alembic alone, and this revision
does two things no other one in this project does: it rewrites student data before adding a
constraint, and it cannot add the constraint at all while duplicates exist.

What is at risk, and checked below:

*The merge.* Duplicate career paths are merged, not deleted. A student who completed a skill on one
copy must still have it completed afterwards -- this is the whole reason the migration is not
"keep the first row and delete the rest".

*The canonical choice.* Which copy survives has to be deterministic and defensible: the one the
student is currently shown, else the one with the most completed work, else the most recently
updated, else the first created. Each rule is exercised on its own.

*The rebuild.* SQLite cannot add a table constraint in place, so batch mode drops career_paths,
recreates it and copies the rows across -- while career_path_phases, career_path_skill_items and
career_path_projects all reference it. A rebuild that broke one of those references would leave a
student's path pointing at nothing, and reading the migration would not show it.

*The constraint.* It has to actually refuse a second row afterwards, on both engines, or nothing
above matters.

The duplicates are seeded through raw SQL at the previous revision, because the models now carry
the constraint: creating this data through them is impossible, which is the point of the phase.
"""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import settings

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

_PREVIOUS_REVISION = "c3a7f1d85e29"
_THIS_REVISION = "5b8e2f7c1a94"
_CONSTRAINT = "uq_career_path_per_role_family"

_FAMILY = "data_analyst"
_NOW = "2026-09-20 12:00:00"

# The query the invariant is stated as, used by the tests and quoted in the phase report.
_DUPLICATE_QUERY = (
    "SELECT profile_id, role_family_key, COUNT(*) FROM career_paths"
    " GROUP BY profile_id, role_family_key HAVING COUNT(*) > 1"
)


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    """A throwaway database this test owns.

    The URL is injected by monkeypatching settings.database_url rather than through the Config:
    alembic/env.py overwrites sqlalchemy.url with settings.database_url on every run, so a URL
    passed through the Config is silently ignored and the migrations run against the developer's
    real database. Mirrors the fixture in test_manual_job_listing_migration.py.
    """
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"career_path_uniqueness_{uuid.uuid4().hex[:8]}"
        admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        url = base.set(database=name).render_as_string(hide_password=False)
        monkeypatch.setattr(settings, "database_url", url)
        yield url
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
    else:
        url = f"sqlite:///{tmp_path / 'career_path_uniqueness.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
    """Programmatic, never Config("alembic.ini"): passing a filename makes alembic/env.py call
    fileConfig(), which reconfigures logging process-wide and silences unrelated tests. Same
    reasoning as the other migration tests."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


def _at_previous_revision(url: str):
    command.upgrade(_alembic_config(), _PREVIOUS_REVISION)
    return create_engine(url)


def _upgrade():
    command.upgrade(_alembic_config(), _THIS_REVISION)


def _rows(url: str, sql: str, **params):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql), params).all()
    finally:
        engine.dispose()


def _seed_profiles(connection, count: int) -> None:
    for index in range(1, count + 1):
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active, created_at, token_version)"
                " VALUES (:id, :email, 'x', :active, :now, 0)"
            ),
            {"id": index, "email": f"student{index}@example.com", "active": True, "now": _NOW},
        )
        connection.execute(
            text("INSERT INTO student_profiles (id, user_id) VALUES (:id, :id)"), {"id": index}
        )


def _seed_practice_blueprint(connection, blueprint_id: int, profile_id: int = 1) -> None:
    """A real blueprint for a skill item's practice link to point at.

    Seeded rather than faked because the migration moves that link onto the canonical row, and
    _assert_no_orphans asks SQLite to check every foreign key afterwards -- an id nothing backs
    would be reported there, as it was the first time this test ran.
    """
    connection.execute(
        text(
            "INSERT INTO assessment_blueprints (id, role_id, jd_id, profile_id, source, sections)"
            " VALUES (:id, NULL, NULL, :pid, 'career_path', '[]')"
        ),
        {"id": blueprint_id, "pid": profile_id},
    )


def _seed_path(
    connection,
    *,
    path_id: int,
    profile_id: int = 1,
    family: str = _FAMILY,
    is_primary: bool = False,
    updated_at: str = _NOW,
    phases: tuple = ("foundations", "core_skills"),
    skills: tuple = (),
    projects: tuple = (),
) -> None:
    """One career path as it exists before this revision, with the children a real one has.

    skills entries are (skill_key, status, completed_via, practice_blueprint_id); projects entries
    are (project_key, status).
    """
    connection.execute(
        text(
            "INSERT INTO career_paths (id, profile_id, role_id, custom_role, role_family_key,"
            " display_role_name, is_primary, generated_via, role_overview, created_at, updated_at)"
            " VALUES (:id, :pid, NULL, :role, :family, :display, :primary, 'catalog', :overview,"
            " :created, :updated)"
        ),
        {
            "id": path_id,
            "pid": profile_id,
            "role": "Data Analyst",
            "family": family,
            "display": "Data Analyst",
            "primary": is_primary,
            "overview": '{"display_name": "Data Analyst"}',
            "created": _NOW,
            "updated": updated_at,
        },
    )
    for order, phase_key in enumerate(phases):
        connection.execute(
            text(
                "INSERT INTO career_path_phases (career_path_id, phase_key, order_index, title, objective)"
                " VALUES (:pid, :key, :order, :title, 'objective')"
            ),
            {"pid": path_id, "key": phase_key, "order": order, "title": phase_key.title()},
        )
    for order, (skill_key, status, completed_via, blueprint_id) in enumerate(skills):
        connection.execute(
            text(
                "INSERT INTO career_path_skill_items (career_path_id, phase_key, skill_key, title,"
                " level, why_it_matters, order_index, practice_blueprint_id, status, completed_via)"
                " VALUES (:pid, 'foundations', :key, :title, 'beginner', 'why', :order, :blueprint,"
                " :status, :via)"
            ),
            {
                "pid": path_id,
                "key": skill_key,
                "title": skill_key.replace("_", " ").title(),
                "order": order,
                "blueprint": blueprint_id,
                "status": status,
                "via": completed_via,
            },
        )
    for order, (project_key, status) in enumerate(projects):
        connection.execute(
            text(
                "INSERT INTO career_path_projects (career_path_id, project_key, title, difficulty,"
                " skills_demonstrated, technologies, interview_questions, order_index, status)"
                " VALUES (:pid, :key, :title, 'beginner', '[]', '[]', '[]', :order, :status)"
            ),
            {"pid": path_id, "key": project_key, "title": project_key.title(), "order": order, "status": status},
        )
    if _TEST_DATABASE_URL:
        # Seeding an explicit id does not advance PostgreSQL's sequence, so the next insert that
        # lets the database choose one would collide with a row seeded here. Production never
        # supplies an id, so this keeps the harness from inventing a failure production cannot have.
        connection.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('career_paths', 'id'),"
                " (SELECT MAX(id) FROM career_paths))"
            )
        )


def _timestamp(value) -> str:
    """One representation of an instant: SQLite hands back the stored string, PostgreSQL a
    datetime, and only the trailing microseconds differ between them."""
    text_value = value if isinstance(value, str) else value.isoformat(sep=" ")
    return text_value.split(".")[0]


def _skills(url: str, path_id: int) -> dict:
    return {
        row.skill_key: (row.status, row.completed_via, row.practice_blueprint_id)
        for row in _rows(
            url,
            "SELECT skill_key, status, completed_via, practice_blueprint_id FROM career_path_skill_items"
            " WHERE career_path_id = :pid",
            pid=path_id,
        )
    }


def _assert_no_orphans(url: str) -> None:
    """Every child points at a career path that exists, and on SQLite the engine's own check agrees.

    SQLite does not enforce foreign keys unless asked, and Alembic's migration connection does not
    ask, so a rebuild that dropped a reference would be silently accepted here and would only fail
    in production. pragma foreign_key_check is the direct question.
    """
    for table in ("career_path_phases", "career_path_skill_items", "career_path_projects"):
        orphans = _rows(
            url,
            f"SELECT COUNT(*) FROM {table} child WHERE NOT EXISTS"
            " (SELECT 1 FROM career_paths p WHERE p.id = child.career_path_id)",
        )
        assert orphans[0][0] == 0, f"{table} has {orphans[0][0]} row(s) pointing at a deleted career path"
    if not _TEST_DATABASE_URL:
        assert _rows(url, "pragma foreign_key_check") == [], "SQLite reports a broken foreign key"


def _assert_indexes_and_foreign_keys_survived(url: str) -> None:
    """The rebuild must not quietly drop what was already on the table.

    SQLite adds a table constraint by recreating the table, and career_paths carries two indexes
    and two foreign keys. test_migration_model_parity deliberately does not treat index or
    constraint differences as drift, so a lost index would pass every other test here and show up
    only as a slow query in production.
    """
    inspector = inspect(create_engine(url))
    indexed = {tuple(index["column_names"]) for index in inspector.get_indexes("career_paths")}
    assert ("profile_id",) in indexed, f"the profile_id index was lost in the rebuild: {indexed}"
    assert ("role_family_key",) in indexed, f"the role_family_key index was lost in the rebuild: {indexed}"
    referred = {
        (key["referred_table"], tuple(key["constrained_columns"]))
        for key in inspector.get_foreign_keys("career_paths")
    }
    assert ("student_profiles", ("profile_id",)) in referred, f"the owning profile's foreign key was lost: {referred}"
    assert ("roles", ("role_id",)) in referred, f"the role foreign key was lost: {referred}"


def _assert_constraint_refuses_duplicates(url: str, profile_id: int = 1, family: str = _FAMILY) -> None:
    engine = create_engine(url)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO career_paths (profile_id, role_family_key, display_role_name,"
                        " is_primary, generated_via, role_overview, created_at, updated_at)"
                        " VALUES (:pid, :family, 'Data Analyst', :primary, 'catalog', :overview, :now, :now)"
                    ),
                    {"pid": profile_id, "family": family, "primary": False, "overview": "{}", "now": _NOW},
                )
    finally:
        engine.dispose()


def test_the_constraint_exists_and_refuses_a_second_row_for_the_same_role(migration_target):
    """The invariant itself, on a database with nothing to merge."""
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 1)
            _seed_path(connection, path_id=1, is_primary=True, skills=(("sql", "completed", "manual", None),))
    finally:
        engine.dispose()
    assert _CONSTRAINT not in {c["name"] for c in inspect(create_engine(migration_target)).get_unique_constraints("career_paths")}

    _upgrade()

    constraints = inspect(create_engine(migration_target)).get_unique_constraints("career_paths")
    matching = [c for c in constraints if c["name"] == _CONSTRAINT]
    assert matching, f"constraint missing; found {[c['name'] for c in constraints]}"
    assert matching[0]["column_names"] == ["profile_id", "role_family_key"], (
        "the constraint must cover the profile as well as the role -- on the role alone it would "
        f"refuse one student's selection because another chose it: {matching[0]}"
    )
    assert _rows(migration_target, _DUPLICATE_QUERY) == []
    # The untouched row is exactly as it was, progress included.
    assert _skills(migration_target, 1) == {"sql": ("completed", "manual", None)}
    assert _rows(migration_target, "SELECT COUNT(*) FROM career_paths")[0][0] == 1
    _assert_no_orphans(migration_target)
    _assert_indexes_and_foreign_keys_survived(migration_target)
    _assert_constraint_refuses_duplicates(migration_target)


def test_a_different_profile_or_role_is_never_refused(migration_target):
    """The other half of the constraint: same role for another student, another role for the same
    student. Both are legitimate and must remain insertable."""
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 2)
            _seed_path(connection, path_id=1, profile_id=1, is_primary=True)
    finally:
        engine.dispose()

    _upgrade()

    engine = create_engine(migration_target)
    try:
        with engine.begin() as connection:
            for profile_id, family in ((2, _FAMILY), (1, "data_scientist")):
                connection.execute(
                    text(
                        "INSERT INTO career_paths (profile_id, role_family_key, display_role_name,"
                        " is_primary, generated_via, role_overview, created_at, updated_at)"
                        " VALUES (:pid, :family, 'Role', :primary, 'catalog', :overview, :now, :now)"
                    ),
                    {"pid": profile_id, "family": family, "primary": False, "overview": "{}", "now": _NOW},
                )
    finally:
        engine.dispose()

    assert _rows(migration_target, "SELECT COUNT(*) FROM career_paths")[0][0] == 3
    assert _rows(migration_target, _DUPLICATE_QUERY) == []


def test_duplicates_are_merged_and_no_completed_work_is_lost(migration_target):
    """The failure the phase is about, as it would be found in a real database.

    Two rows for one role, each with progress the other does not have, and a skill both carry with
    different statuses. Afterwards there is one row, and every piece of completed work is on it.
    """
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 1)
            _seed_practice_blueprint(connection, 77)
            # The path the student is being shown. Less progress than the copy below, which is
            # exactly the case where "keep the row with the most work" would take the wrong one.
            _seed_path(
                connection,
                path_id=1,
                is_primary=True,
                updated_at="2026-09-21 09:00:00",
                skills=(
                    ("sql", "in_progress", None, None),
                    ("excel", "not_started", None, None),
                ),
                projects=(("dashboard", "in_progress"),),
            )
            # The copy an overlapping selection created. The student worked on this one too.
            _seed_path(
                connection,
                path_id=2,
                updated_at="2026-09-22 18:30:00",
                skills=(
                    ("sql", "completed", "assessment", 77),
                    ("python", "completed", "manual", None),
                ),
                projects=(("dashboard", "completed"), ("forecast", "completed")),
            )
    finally:
        engine.dispose()
    assert [tuple(r) for r in _rows(migration_target, _DUPLICATE_QUERY)] == [(1, _FAMILY, 2)]

    _upgrade()

    assert _rows(migration_target, _DUPLICATE_QUERY) == [], "duplicates survived the migration"
    remaining = _rows(migration_target, "SELECT id, is_primary, updated_at FROM career_paths")
    assert len(remaining) == 1
    assert remaining[0].id == 1, "the path the student was being shown must be the one that survives"
    assert bool(remaining[0].is_primary) is True
    # The canonical row carries the latest activity on this role, so the saved-paths list -- which
    # is ordered by updated_at -- still shows it where the student expects.
    assert _timestamp(remaining[0].updated_at) == "2026-09-22 18:30:00"

    skills = _skills(migration_target, 1)
    assert skills["sql"] == ("completed", "assessment", 77), "the further-along status and its evidence were lost"
    assert skills["python"] == ("completed", "manual", None), "a skill only the duplicate had was lost"
    assert skills["excel"] == ("not_started", None, None), "an untouched skill changed"
    assert len(skills) == 3, f"skill items were duplicated rather than merged: {sorted(skills)}"

    projects = dict(
        _rows(migration_target, "SELECT project_key, status FROM career_path_projects WHERE career_path_id = 1")
    )
    assert projects == {"dashboard": "completed", "forecast": "completed"}
    # One set of phases, not two.
    phases = _rows(migration_target, "SELECT phase_key FROM career_path_phases WHERE career_path_id = 1")
    assert sorted(p.phase_key for p in phases) == ["core_skills", "foundations"]
    assert _rows(migration_target, "SELECT COUNT(*) FROM career_path_phases")[0][0] == 2

    _assert_no_orphans(migration_target)
    _assert_indexes_and_foreign_keys_survived(migration_target)
    _assert_constraint_refuses_duplicates(migration_target)


def test_the_moved_items_keep_a_usable_order_within_their_phase(migration_target):
    """A skill moved across must not collide with the canonical row's own ordering, or the phase
    renders two items in the same position."""
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 1)
            _seed_path(connection, path_id=1, is_primary=True, skills=(("sql", "in_progress", None, None),))
            _seed_path(
                connection,
                path_id=2,
                skills=(("python", "completed", "manual", None), ("r", "in_progress", None, None)),
            )
    finally:
        engine.dispose()

    _upgrade()

    items = _rows(
        migration_target,
        "SELECT skill_key, phase_key, order_index FROM career_path_skill_items"
        " WHERE career_path_id = 1 ORDER BY order_index",
    )
    assert [i.skill_key for i in items] == ["sql", "python", "r"], "moved items were not appended after the existing one"
    assert len({(i.phase_key, i.order_index) for i in items}) == len(items), "two items share a position in one phase"


def test_without_a_primary_copy_the_one_with_the_most_completed_work_survives(migration_target):
    """Rule 2. Neither copy is the current path -- the student selected something else since --
    so the surviving row is chosen by the work on it, not by its id or its timestamp."""
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 1)
            _seed_path(
                connection,
                path_id=1,
                updated_at="2026-09-25 10:00:00",
                skills=(("sql", "in_progress", None, None),),
            )
            _seed_path(
                connection,
                path_id=2,
                updated_at="2026-09-21 10:00:00",
                skills=(("sql", "completed", "assessment", None), ("python", "completed", "manual", None)),
                projects=(("dashboard", "completed"),),
            )
            # A third copy, to prove the rule holds beyond a pair.
            _seed_path(connection, path_id=3, updated_at="2026-09-19 10:00:00")
    finally:
        engine.dispose()
    assert [tuple(r) for r in _rows(migration_target, _DUPLICATE_QUERY)] == [(1, _FAMILY, 3)]

    _upgrade()

    remaining = _rows(migration_target, "SELECT id, is_primary FROM career_paths")
    assert [r.id for r in remaining] == [2], "the copy holding the student's completed work was not kept"
    assert bool(remaining[0].is_primary) is False, "the migration must not invent a current path"
    skills = _skills(migration_target, 2)
    assert skills["sql"] == ("completed", "assessment", None)
    assert skills["python"] == ("completed", "manual", None)
    assert _rows(migration_target, _DUPLICATE_QUERY) == []
    _assert_no_orphans(migration_target)


def test_with_equal_work_the_most_recently_updated_survives(migration_target):
    """Rule 3, and rule 4 underneath it: with nothing to choose between the copies' progress, the
    latest one wins, and two identical copies fall back to the first created."""
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 2)
            _seed_path(connection, path_id=1, updated_at="2026-09-20 08:00:00")
            _seed_path(connection, path_id=2, updated_at="2026-09-24 08:00:00")
            # A second student whose copies are indistinguishable: same timestamp, no progress.
            _seed_path(connection, path_id=3, profile_id=2, updated_at=_NOW)
            _seed_path(connection, path_id=4, profile_id=2, updated_at=_NOW)
    finally:
        engine.dispose()

    _upgrade()

    assert [r.id for r in _rows(migration_target, "SELECT id FROM career_paths WHERE profile_id = 1")] == [2]
    assert [r.id for r in _rows(migration_target, "SELECT id FROM career_paths WHERE profile_id = 2")] == [3]
    assert _rows(migration_target, _DUPLICATE_QUERY) == []


def test_two_copies_marked_primary_leave_exactly_one(migration_target):
    """The state the unfixed PostgreSQL path actually produced: every overlapping request marked
    its own row primary. Afterwards the role has one row, and it is the current one."""
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 1)
            _seed_path(connection, path_id=1, is_primary=True, skills=(("sql", "completed", "manual", None),))
            _seed_path(connection, path_id=2, is_primary=True)
    finally:
        engine.dispose()
    assert _rows(migration_target, "SELECT COUNT(*) FROM career_paths WHERE is_primary")[0][0] == 2

    _upgrade()

    remaining = _rows(migration_target, "SELECT id, is_primary FROM career_paths")
    assert len(remaining) == 1
    assert bool(remaining[0].is_primary) is True
    assert remaining[0].id == 1, "the copy with the student's completed skill was not the one kept"
    assert _skills(migration_target, 1)["sql"] == ("completed", "manual", None)


def test_each_profile_and_role_is_merged_independently(migration_target):
    """Duplicates in several places at once: two students, two roles, and a role that was never
    duplicated. Each group resolves on its own, and nothing crosses between them."""
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 2)
            _seed_path(connection, path_id=1, profile_id=1, is_primary=True, skills=(("sql", "completed", "manual", None),))
            _seed_path(connection, path_id=2, profile_id=1, skills=(("python", "completed", "manual", None),))
            _seed_path(
                connection, path_id=3, profile_id=1, family="data_scientist",
                skills=(("stats", "in_progress", None, None),),
            )
            _seed_path(connection, path_id=4, profile_id=2, family="sde", skills=(("dsa", "completed", "assessment", None),))
            _seed_path(connection, path_id=5, profile_id=2, family="sde", is_primary=True)
    finally:
        engine.dispose()
    assert len(_rows(migration_target, _DUPLICATE_QUERY)) == 2

    _upgrade()

    assert _rows(migration_target, _DUPLICATE_QUERY) == []
    stored = _rows(migration_target, "SELECT id, profile_id, role_family_key FROM career_paths")
    kept = {(row.profile_id, row.role_family_key): row.id for row in stored}
    assert kept == {(1, _FAMILY): 1, (1, "data_scientist"): 3, (2, "sde"): 5}
    assert _skills(migration_target, 1) == {
        "sql": ("completed", "manual", None),
        "python": ("completed", "manual", None),
    }, "the second student's merge or the other role's rows leaked into this one"
    assert _skills(migration_target, 3) == {"stats": ("in_progress", None, None)}
    assert _skills(migration_target, 5) == {"dsa": ("completed", "assessment", None)}
    _assert_no_orphans(migration_target)


def test_the_migration_is_reversible_and_the_merge_stands(migration_target):
    """Downgrade drops the constraint so the previous release can run again. The merged rows stay
    merged: a downgrade reverses a schema change, and there is no honest way to re-split a student's
    progress back onto rows that no longer mean anything. OPERATIONS.md says the same -- a
    downgrade is not a data restore."""
    engine = _at_previous_revision(migration_target)
    try:
        with engine.begin() as connection:
            _seed_profiles(connection, 1)
            _seed_path(connection, path_id=1, is_primary=True, skills=(("sql", "completed", "manual", None),))
            _seed_path(connection, path_id=2, skills=(("python", "completed", "manual", None),))
    finally:
        engine.dispose()
    _upgrade()
    assert _rows(migration_target, "SELECT COUNT(*) FROM career_paths")[0][0] == 1

    command.downgrade(_alembic_config(), _PREVIOUS_REVISION)

    assert _CONSTRAINT not in {
        c["name"] for c in inspect(create_engine(migration_target)).get_unique_constraints("career_paths")
    }
    assert _skills(migration_target, 1) == {
        "sql": ("completed", "manual", None),
        "python": ("completed", "manual", None),
    }
    _assert_no_orphans(migration_target)

    _upgrade()

    assert _rows(migration_target, _DUPLICATE_QUERY) == []
    _assert_constraint_refuses_duplicates(migration_target)
