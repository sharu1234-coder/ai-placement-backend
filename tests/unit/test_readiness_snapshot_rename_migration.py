"""The Phase 2G column rename, run rather than read.

The rest of the suite builds its schema with `create_all` from the models, which would pass
just as happily if this migration were empty. Production is built by Alembic alone, so the
only way to know a rename preserves data is to put real rows in, run it, and look.

What is actually at risk here, and why each is checked below:

*The values.* These rows are historical measurements taken on real days. A rename that
recreated the column instead of renaming it -- which is what SQLite's batch mode does under
the hood -- could silently drop or reorder them. A 0 in this column means a student was
genuinely measured at 0% coverage that day; if it came back as 37, or as NULL, the trend line
would report an improvement that never happened.

*NOT NULL.* The constraint is what makes a row mean "a measurement was taken". Phase 2F relies
on it: Career Twin writes no row at all when the value is unmeasurable, rather than storing a
0 that a later trend reads back as a real past score.

*Reversibility.* A rename that cannot be undone is a one-way door on production data.

*The unique constraint and the indexes.* uq_readiness_snapshot_per_day is the concurrency
arbiter for two same-day reads of one Career Twin. A batch rebuild that quietly dropped it
would leave the race test passing for the wrong reason.
"""

import datetime
import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import settings

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

_TABLE = "readiness_snapshots"
_PREVIOUS_REVISION = "9a2f7be31c04"
_THIS_REVISION = "b4e17c9d05af"

# Deliberately includes both ends of the scale. 0 is the one that matters most: it is a
# measured zero, the exact value Phase 2F fought to keep distinguishable from "unknown".
_SEEDED = [
    (1, "sde", datetime.date(2026, 1, 1), 0),
    (2, "sde", datetime.date(2026, 1, 8), 37),
    (3, "data_analyst", datetime.date(2026, 1, 8), 100),
]


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"snapshot_rename_migration_test_{uuid.uuid4().hex[:8]}"
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
        url = f"sqlite:///{tmp_path / 'snapshot_rename.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


def _columns(url: str) -> dict:
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns(_TABLE)}
    finally:
        engine.dispose()


def _seed(url: str) -> None:
    """Rows exactly as they exist before this revision, under the OLD column name."""
    engine = create_engine(url)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    insert_snapshot = (
        "INSERT INTO readiness_snapshots"
        " (id, profile_id, role_family_key, snapshot_date, readiness_percent,"
        "  per_skill_confidence_percent, created_at)"
        " VALUES (:id, 1, :role, :day, :percent, :per_skill, :now)"
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, is_active, created_at, token_version)"
                    " VALUES (1, 'pre@example.com', 'x', 1, :now, 0)"
                ),
                {"now": now},
            )
            connection.execute(text("INSERT INTO student_profiles (id, user_id) VALUES (1, 1)"))
            for row_id, role_key, day, percent in _SEEDED:
                connection.execute(
                    text(insert_snapshot),
                    {
                        "id": row_id,
                        "role": role_key,
                        "day": day,
                        "percent": percent,
                        "per_skill": "{}",
                        "now": now,
                    },
                )
    finally:
        engine.dispose()


def _read_rows(url: str, column: str):
    engine = create_engine(url)
    query = (
        f"SELECT id, profile_id, role_family_key, snapshot_date, {column}"
        f" FROM {_TABLE} ORDER BY id"
    )
    try:
        with engine.connect() as connection:
            return connection.execute(text(query)).fetchall()
    finally:
        engine.dispose()


@pytest.fixture()
def seeded_at_previous_revision(migration_target):
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed(migration_target)
    return migration_target


def test_the_column_is_renamed_not_replaced(seeded_at_previous_revision):
    command.upgrade(_alembic_config(), _THIS_REVISION)

    columns = _columns(seeded_at_previous_revision)
    assert "evidence_coverage_percent" in columns
    assert "readiness_percent" not in columns, "the old name must be gone, not merely shadowed"


def test_every_historical_value_survives_the_rename(seeded_at_previous_revision):
    """The whole point of the phase: a naming fix must not touch a single measurement."""
    before = _read_rows(seeded_at_previous_revision, "readiness_percent")

    command.upgrade(_alembic_config(), _THIS_REVISION)

    after = _read_rows(seeded_at_previous_revision, "evidence_coverage_percent")
    assert after == before
    assert [row[4] for row in after] == [0, 37, 100]
    assert len(after) == len(_SEEDED), "no row may be lost or duplicated"


def test_a_measured_zero_is_still_a_zero_afterwards(seeded_at_previous_revision):
    """Called out on its own because it is the value a careless rebuild is most likely to
    mangle into NULL -- and NULL here would mean "never measured", which is a different and
    false statement about that student's history."""
    command.upgrade(_alembic_config(), _THIS_REVISION)

    rows = _read_rows(seeded_at_previous_revision, "evidence_coverage_percent")
    assert rows[0][4] == 0
    assert rows[0][4] is not None


def test_not_null_is_preserved(seeded_at_previous_revision):
    command.upgrade(_alembic_config(), _THIS_REVISION)

    assert _columns(seeded_at_previous_revision)["evidence_coverage_percent"]["nullable"] is False


def test_the_unique_constraint_and_indexes_survive(seeded_at_previous_revision):
    """SQLite's batch mode rebuilds the table; a rebuild that forgot these would leave the
    same-day concurrency guarantee gone and the race test green for the wrong reason."""
    command.upgrade(_alembic_config(), _THIS_REVISION)

    engine = create_engine(seeded_at_previous_revision)
    try:
        inspector = inspect(engine)
        unique_names = {u["name"] for u in inspector.get_unique_constraints(_TABLE)}
        indexed = {tuple(i["column_names"]) for i in inspector.get_indexes(_TABLE)}
    finally:
        engine.dispose()

    assert "uq_readiness_snapshot_per_day" in unique_names
    for column in ("profile_id", "role_family_key", "snapshot_date"):
        assert (column,) in indexed, f"index on {column} was lost in the rebuild"


def test_the_rename_is_reversible_with_the_values_intact(seeded_at_previous_revision):
    config = _alembic_config()
    before = _read_rows(seeded_at_previous_revision, "readiness_percent")

    command.upgrade(config, _THIS_REVISION)
    command.downgrade(config, _PREVIOUS_REVISION)

    columns = _columns(seeded_at_previous_revision)
    assert "readiness_percent" in columns
    assert "evidence_coverage_percent" not in columns
    assert _read_rows(seeded_at_previous_revision, "readiness_percent") == before


def test_upgrading_again_after_a_downgrade_still_works(seeded_at_previous_revision):
    """A round trip, because a rename that only survives one direction is a trap waiting for
    whoever has to roll back a deploy."""
    config = _alembic_config()
    before = _read_rows(seeded_at_previous_revision, "readiness_percent")

    command.upgrade(config, _THIS_REVISION)
    command.downgrade(config, _PREVIOUS_REVISION)
    command.upgrade(config, _THIS_REVISION)

    columns = _columns(seeded_at_previous_revision)
    assert "evidence_coverage_percent" in columns
    assert "readiness_percent" not in columns
    assert _read_rows(seeded_at_previous_revision, "evidence_coverage_percent") == before


def test_the_migration_produces_the_nullability_the_model_declares(migration_target):
    """The models are the living description of the schema; head must agree with them."""
    command.upgrade(_alembic_config(), "head")

    from app.models.readiness_snapshot import ReadinessSnapshot

    column = _columns(migration_target)["evidence_coverage_percent"]
    assert column["nullable"] is ReadinessSnapshot.__table__.c.evidence_coverage_percent.nullable
