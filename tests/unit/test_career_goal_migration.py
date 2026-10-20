"""The career_goals migration, run rather than read.

Same reasoning as the three sibling migration tests: every other test builds its schema with
`Base.metadata.create_all`, which reads the models and would pass just as happily if this
migration were empty. Production is built by Alembic alone.

What matters here is narrower than "does the table appear". A career goal points at an
optional JobDescription, and the whole create-before-analyze design depends on that column
genuinely accepting NULL -- if the migration made it NOT NULL, a goal could not exist until an
AI call had succeeded, which is precisely the failure mode the design avoids.
"""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import settings
from tests import db_safety

# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()

_TABLE = "career_goals"
_PREVIOUS_REVISION = "0d6496be2213"
_THIS_REVISION = "7c41a0d9e2b5"


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    """A throwaway database this test owns -- it runs downgrades, which would wreck the suite's
    shared one. Mirrors the fixture in the sibling migration tests."""
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"career_goal_migration_test_{uuid.uuid4().hex[:8]}"
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
        url = f"sqlite:///{tmp_path / 'career_goal_migration.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
    """Programmatic, never Config("alembic.ini") -- see the sibling tests for why."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


def _columns(url: str) -> dict:
    engine = db_safety.make_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns(_TABLE)}
    finally:
        engine.dispose()


def _current_revision(url: str):
    engine = db_safety.make_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            return row[0] if row else None
    finally:
        engine.dispose()


def test_the_migration_creates_the_table_with_the_nullability_the_design_needs(migration_target):
    command.upgrade(_alembic_config(), _THIS_REVISION)

    columns = _columns(migration_target)
    # A goal is nothing without these, so they must be refused at the database as well as in
    # the schema.
    for required in ("profile_id", "title", "company", "job_description", "status"):
        assert columns[required]["nullable"] is False, f"{required} must be NOT NULL"
    # And these two carry the design's two honest absences: a posting with no stated location,
    # and a goal that has never been analyzed.
    assert columns["location"]["nullable"] is True
    assert columns["jd_id"]["nullable"] is True, (
        "a career goal must be creatable before any AI call has succeeded"
    )
    assert _current_revision(migration_target) == _THIS_REVISION


def test_the_migration_produces_the_nullability_the_model_declares(migration_target):
    """Guards against the migration and the model drifting apart, since everywhere else in the
    suite the schema comes from the model and the two are never compared.

    Upgrades to "head" rather than to _THIS_REVISION, unlike its two neighbours above. Those
    two assert what 7c41a0d9e2b5 itself did and must stay pinned to it; this one compares the
    live model against the schema, and the model always describes the LATEST schema. Pinning it
    to a historical revision made it fail the moment a later migration added a column
    (Phase 2E's saved_job_id) -- correctly, but for a reason that is about the pin rather than
    about any drift.
    """
    from app.models.career_goal import CareerGoal

    command.upgrade(_alembic_config(), "head")

    migrated = _columns(migration_target)
    for column in CareerGoal.__table__.columns:
        if column.primary_key:
            continue
        assert column.name in migrated, f"model declares {column.name}, the migration does not create it"
        assert migrated[column.name]["nullable"] == column.nullable, (
            f"{column.name}: model says nullable={column.nullable}, "
            f"migration produced nullable={migrated[column.name]['nullable']}"
        )


def test_the_migration_round_trips(migration_target):
    """A downgrade that leaves something behind fails only on the next upgrade, which in
    practice means during a rollback."""
    config = _alembic_config()
    command.upgrade(config, _THIS_REVISION)

    engine = db_safety.make_engine(migration_target)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (email, password_hash, created_at, is_active, token_version) "
                    "VALUES ('goal-migration@example.com', 'x', CURRENT_TIMESTAMP, :active, 0)"
                ),
                {"active": True},
            )
            user_id = connection.execute(text("SELECT MAX(id) FROM users")).scalar()
            connection.execute(
                text("INSERT INTO student_profiles (user_id) VALUES (:user_id)"), {"user_id": user_id}
            )
            profile_id = connection.execute(text("SELECT MAX(id) FROM student_profiles")).scalar()
            # A goal with no analysis attached -- the state the design depends on.
            connection.execute(
                text(
                    "INSERT INTO career_goals "
                    "(profile_id, title, company, location, job_description, jd_id, status, "
                    " created_at, updated_at) "
                    "VALUES (:profile_id, 'Data Engineer', 'Microsoft', NULL, 'a posting', NULL, "
                    "        'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"profile_id": profile_id},
            )
            stored = connection.execute(
                text("SELECT jd_id, location, status FROM career_goals")
            ).fetchone()
        assert stored == (None, None, "active")
    finally:
        engine.dispose()

    command.downgrade(config, _PREVIOUS_REVISION)
    assert _current_revision(migration_target) == _PREVIOUS_REVISION
    engine = db_safety.make_engine(migration_target)
    try:
        assert _TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, _THIS_REVISION)
    assert _current_revision(migration_target) == _THIS_REVISION
    assert "jd_id" in _columns(migration_target)
