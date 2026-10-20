"""The Phase 26 migration, run against real duplicate active paths rather than read.

Every other test in this suite builds its schema with create_all from the models, which would
pass identically if this migration were empty. Production is built by Alembic alone, and this
revision rewrites student-visible state before creating an index that cannot exist while
duplicates do.

What is at risk, and checked below:

*Nothing is deleted.* Unlike the Phase 25 preparation-plan migration, extra rows here are marked
`superseded`, not removed -- a placement path owns placement_path_topics rows that carry the
student's own progress (topic status, completed_via, the practice blueprint they were handed).
The central test asserts that every one of those rows survives the migration byte for byte,
including on the paths that stopped being active.

*The canonical choice.* The row that stays active has to be the one the student last asked for,
and it has to be deterministic -- the read path it replaces used an unordered `.first()`, so
before this revision the database chose.

*The predicate.* A plain UNIQUE(profile_id) would pass a naive "it refuses a duplicate" test and
destroy the product: superseded paths are history that a profile accumulates for good. So the
index must refuse a second *active* row and permit any number of superseded ones.

*Reversibility.* The downgrade drops the index and does not resurrect anything, which is asserted
rather than left implied.

The duplicates are seeded through raw SQL at the previous revision, because the model now carries
the index: creating this data through it is impossible, which is the point of the phase.
"""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import settings
from tests import db_safety

# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()

_PREVIOUS_REVISION = "b1c47e0a52d9"
_THIS_REVISION = "c9e2f5a71b04"
_INDEX = "uq_placement_path_one_active_per_profile"

_NOW = "2026-10-01 12:00:00"

# The query the invariant is stated as, used by the tests and quoted in the phase report.
_DUPLICATE_QUERY = (
    "SELECT profile_id, COUNT(*) FROM placement_paths WHERE status = 'active'"
    " GROUP BY profile_id HAVING COUNT(*) > 1"
)


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    """A throwaway database this test owns.

    The URL is injected by monkeypatching settings.database_url rather than through the Config:
    alembic/env.py overwrites sqlalchemy.url with settings.database_url on every run, so a URL
    passed through the Config is silently ignored and the migrations run against the developer's
    real database. Mirrors the other migration tests.
    """
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"placement_path_uniqueness_{uuid.uuid4().hex[:8]}"
        admin = db_safety.make_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        url = db_safety.hidden(
            base.set(database=name).render_as_string(hide_password=False)
        )
        monkeypatch.setattr(settings, "database_url", url)
        try:
            yield url
        finally:
            # In a finally, so a test that errors cannot leave the database behind, and
            # without FORCE -- see db_safety.drop_temp_database for why FORCE needs
            # privileges over the superuser that this role deliberately does not have.
            db_safety.drop_temp_database(admin, name)
    else:
        url = f"sqlite:///{tmp_path / 'placement_path_uniqueness.db'}"
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
    return db_safety.make_engine(url)


def _upgrade():
    command.upgrade(_alembic_config(), _THIS_REVISION)


def _rows(url: str, sql: str, **params):
    engine = db_safety.make_engine(url)
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


def _seed_path(connection, path_id: int, profile_id: int, status: str) -> None:
    connection.execute(
        text(
            "INSERT INTO placement_paths (id, profile_id, status, created_at)"
            " VALUES (:id, :pid, :status, :now)"
        ),
        {"id": path_id, "pid": profile_id, "status": status, "now": _NOW},
    )


def _seed_blueprint(connection, blueprint_id: int, profile_id: int) -> None:
    """The parent row a topic's practice_blueprint_id points at.

    SQLite does not enforce foreign keys unless asked to, so seeding the child alone passed
    there for as long as this test has existed and raised ForeignKeyViolation the first time
    it met PostgreSQL. The parent belongs to the fixture's truth rather than being a
    workaround: a topic completed via an assessment really does reference a blueprint.

    The columns are the ones the table has had since d36dd7ceee4e created it -- id, role_id,
    jd_id, profile_id, source, sections -- so this is valid at the revision under test.
    """
    connection.execute(
        text(
            "INSERT INTO assessment_blueprints (id, profile_id, source, sections)"
            " VALUES (:id, :pid, 'default_taxonomy', :sections)"
        ),
        {"id": blueprint_id, "pid": profile_id, "sections": "[]"},
    )


def _seed_topic(connection, topic_id: int, path_id: int, topic_key: str, status: str,
                completed_via=None, blueprint_id=None) -> None:
    """One topic row, carrying the student's own progress -- which is what must not be lost."""
    connection.execute(
        text(
            "INSERT INTO placement_path_topics"
            " (id, path_id, stage, track_key, topic_key, order_in_track, title, skill_tag,"
            "  status, completed_via, practice_blueprint_id)"
            " VALUES (:id, :path, 'foundation', 'data', :key, 1, :title, :skill,"
            "         :status, :via, :bp)"
        ),
        {
            "id": topic_id, "path": path_id, "key": topic_key, "title": f"Topic {topic_key}",
            "skill": "SQL", "status": status, "via": completed_via, "bp": blueprint_id,
        },
    )


def _paths(url: str):
    return _rows(url, "SELECT id, profile_id, status FROM placement_paths ORDER BY id")


def _topics(url: str):
    return _rows(
        url,
        "SELECT id, path_id, topic_key, status, completed_via, practice_blueprint_id"
        " FROM placement_path_topics ORDER BY id",
    )


def _assert_index_refuses_a_second_active_path(url: str, profile_id: int = 1) -> None:
    engine = db_safety.make_engine(url)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _seed_path(connection, 999, profile_id, "active")
    finally:
        engine.dispose()


# ==========================================================================================


def test_the_index_exists_and_refuses_a_second_active_path(migration_target):
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 1)
        _seed_path(connection, 1, 1, "active")
    engine.dispose()

    _upgrade()

    indexes = {
        index["name"] for index in inspect(db_safety.make_engine(migration_target)).get_indexes("placement_paths")
    }
    assert _INDEX in indexes, indexes
    _assert_index_refuses_a_second_active_path(migration_target)


def test_superseded_history_is_still_unlimited(migration_target):
    """The reason this is a partial index and not UNIQUE(profile_id).

    A profile accumulates superseded paths for good -- the model never deletes them and
    GET /placement-path/history lists them. A plain unique constraint on profile_id would have
    passed the test above and made regeneration impossible after the first time.
    """
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 1)
        _seed_path(connection, 1, 1, "active")
    engine.dispose()
    _upgrade()

    engine = db_safety.make_engine(migration_target)
    with engine.begin() as connection:
        for path_id in range(2, 8):
            _seed_path(connection, path_id, 1, "superseded")
    engine.dispose()

    stored = _paths(migration_target)
    assert len(stored) == 7
    assert len([row for row in stored if row.status == "active"]) == 1
    _assert_index_refuses_a_second_active_path(migration_target)


def test_duplicate_active_paths_are_superseded_and_no_progress_is_lost(migration_target):
    """The central test: the newest active path stays active, and every topic row survives.

    This is the difference between this migration and Phase 25's. A preparation plan is generated
    prose and a shadowed one can be deleted; a placement path owns the student's own progress, so
    the losing rows are retired rather than removed and their topics are left exactly as they are.
    """
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 1)
        _seed_path(connection, 1, 1, "active")
        _seed_path(connection, 2, 1, "active")
        _seed_path(connection, 5, 1, "active")
        # Progress the student made on each copy, including on the ones about to be retired.
        # Topic 10 was completed via an assessment, so its blueprint parent has to exist.
        _seed_blueprint(connection, 77, 1)
        _seed_topic(connection, 10, 1, "sql_basics", "completed", "assessment", 77)
        _seed_topic(connection, 11, 2, "sql_joins", "in_progress")
        _seed_topic(connection, 12, 5, "sql_basics", "completed", "manual")
    engine.dispose()
    assert len(_rows(migration_target, _DUPLICATE_QUERY)) == 1, "the duplicates were not seeded"
    before_topics = _topics(migration_target)

    _upgrade()

    assert [(row.id, row.status) for row in _paths(migration_target)] == [
        (1, "superseded"),
        (2, "superseded"),
        (5, "active"),
    ]
    assert _rows(migration_target, _DUPLICATE_QUERY) == []
    assert _topics(migration_target) == before_topics, (
        "no topic row may be added, removed or altered -- these carry the student's own progress"
    )
    _assert_index_refuses_a_second_active_path(migration_target)


def test_each_profile_is_resolved_independently(migration_target):
    """Several overlapping duplicate groups at once, which is what a real deployment has.

    A migration that kept one active row per *table* rather than per profile would pass the
    single-profile test above and leave most students with no roadmap at all.
    """
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 3)
        # profile 1: three active
        _seed_path(connection, 1, 1, "active")
        _seed_path(connection, 2, 1, "active")
        _seed_path(connection, 9, 1, "active")
        # profile 2: one active and one already superseded, which must be left alone
        _seed_path(connection, 3, 2, "superseded")
        _seed_path(connection, 4, 2, "active")
        # profile 3: two active
        _seed_path(connection, 5, 3, "active")
        _seed_path(connection, 8, 3, "active")
    engine.dispose()

    _upgrade()

    assert [(row.id, row.profile_id, row.status) for row in _paths(migration_target)] == [
        (1, 1, "superseded"),
        (2, 1, "superseded"),
        (3, 2, "superseded"),
        (4, 2, "active"),
        (5, 3, "superseded"),
        (8, 3, "active"),
        (9, 1, "active"),
    ]
    assert _rows(migration_target, _DUPLICATE_QUERY) == []


def test_a_database_with_no_duplicates_is_left_alone(migration_target):
    """The ordinary deployment -- and the one measured on this machine, which had four profiles
    with a placement path and no profile with more than one active. The migration must still
    create the index rather than skip it because it found nothing to resolve."""
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 2)
        _seed_path(connection, 1, 1, "superseded")
        _seed_path(connection, 2, 1, "active")
        _seed_path(connection, 3, 2, "active")
    engine.dispose()

    _upgrade()

    assert [(row.id, row.status) for row in _paths(migration_target)] == [
        (1, "superseded"),
        (2, "active"),
        (3, "active"),
    ]
    _assert_index_refuses_a_second_active_path(migration_target)


def test_the_migration_is_reversible_and_the_resolution_stands(migration_target):
    """Downgrade drops the index. It does not re-activate anything, and says so.

    Resurrecting a path this migration retired would reintroduce the duplicate, and choosing
    which of two to bring back is not a decision a downgrade can make.
    """
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 1)
        _seed_path(connection, 1, 1, "active")
        _seed_path(connection, 2, 1, "active")
    engine.dispose()
    _upgrade()

    command.downgrade(_alembic_config(), _PREVIOUS_REVISION)

    indexes = {
        index["name"] for index in inspect(db_safety.make_engine(migration_target)).get_indexes("placement_paths")
    }
    assert _INDEX not in indexes, indexes
    assert [(row.id, row.status) for row in _paths(migration_target)] == [
        (1, "superseded"),
        (2, "active"),
    ]
    # And a second active row is accepted again, which is what "the index is gone" means.
    engine = db_safety.make_engine(migration_target)
    with engine.begin() as connection:
        _seed_path(connection, 3, 1, "active")
    engine.dispose()
    assert len([row for row in _paths(migration_target) if row.status == "active"]) == 2
