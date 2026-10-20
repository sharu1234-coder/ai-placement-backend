"""The Phase 2E link migration, run rather than read.

Same reasoning as the sibling migration tests: the rest of the suite builds its schema from the
models with `create_all`, which would pass just as happily if this migration were empty.
Production is built by Alembic alone.

Three things here are worth proving and cannot be seen by reading the model:

*The column accepts NULL and existing rows survive.* Every goal and every saved job that
existed before this revision keeps working, unlinked. A migration that made the column NOT NULL
would break every account in the product on deploy.

*UNIQUE is really there.* It is not decoration -- it is the arbiter the concurrent link test
relies on, so if the batch ALTER silently dropped it the race test would start passing for the
wrong reason.

*ON DELETE SET NULL is really there.* Without it, deleting a saved job raises against a linked
goal, and account deletion (which removes saved_jobs BEFORE career_goals) fails outright.
"""

import datetime
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

_TABLE = "career_goals"
_PREVIOUS_REVISION = "7c41a0d9e2b5"
_THIS_REVISION = "9a2f7be31c04"


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"goal_link_migration_test_{uuid.uuid4().hex[:8]}"
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
        url = f"sqlite:///{tmp_path / 'goal_link_migration.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
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


def _seed_unlinked_rows(url: str) -> None:
    """A profile with a goal and a saved job, exactly as they exist before this revision."""
    engine = db_safety.make_engine(url)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO users (id, email, password_hash, is_active, created_at, token_version)"
                     " VALUES (1, 'pre@example.com', 'x', :active, :now, 0)"),
                {"now": now, "active": True},
            )
            connection.execute(
                text("INSERT INTO student_profiles (id, user_id) VALUES (1, 1)")
            )
            connection.execute(
                text(
                    "INSERT INTO job_listings (id, provider, provider_job_id, url, title,"
                    " salary_is_predicted, fetched_at)"
                    " VALUES (1, 'test', 'p1', 'https://example.invalid/j', 'Data Engineer', FALSE, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO saved_jobs (id, profile_id, listing_id, status, source,"
                    " created_at, updated_at)"
                    " VALUES (1, 1, 1, 'applied', 'manual', :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO career_goals (id, profile_id, title, company, job_description,"
                    " status, created_at, updated_at)"
                    " VALUES (1, 1, 'Data Engineer', 'Microsoft', 'Required: SQL.', 'active',"
                    " :now, :now)"
                ),
                {"now": now},
            )
    finally:
        engine.dispose()


def test_the_column_arrives_nullable_and_existing_rows_survive(migration_target):
    """The deploy-day case: rows that predate the link must come through untouched."""
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_unlinked_rows(migration_target)

    command.upgrade(config, _THIS_REVISION)

    columns = _columns(migration_target)
    assert "saved_job_id" in columns
    assert columns["saved_job_id"]["nullable"] is True, (
        "a goal must be able to exist without an application -- most do"
    )
    engine = db_safety.make_engine(migration_target)
    try:
        with engine.connect() as connection:
            goal = connection.execute(
                text("SELECT title, saved_job_id FROM career_goals WHERE id = 1")
            ).fetchone()
            assert goal[0] == "Data Engineer", "the pre-existing goal was altered"
            assert goal[1] is None, "an existing goal must arrive unlinked, not guessed at"
            assert connection.execute(text("SELECT COUNT(*) FROM saved_jobs")).scalar() == 1
    finally:
        engine.dispose()
    assert _current_revision(migration_target) == _THIS_REVISION


def test_the_unique_constraint_really_exists(migration_target):
    """Proven by the database refusing the second write, not by reading metadata -- a unique
    constraint that inspect() reports but the engine does not enforce would be worse than none,
    because the concurrency test would pass while the product rule broke."""
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_unlinked_rows(migration_target)
    command.upgrade(config, _THIS_REVISION)

    engine = db_safety.make_engine(migration_target)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO career_goals (id, profile_id, title, company, job_description,"
                    " status, created_at, updated_at, saved_job_id)"
                    " VALUES (2, 1, 'Second', 'Microsoft', 'x', 'active', :now, :now, 1)"
                ),
                {"now": now},
            )
        # IntegrityError specifically, not a blind Exception: a typo in the SQL above would
        # also raise, and would then be indistinguishable from the constraint doing its job.
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO career_goals (id, profile_id, title, company,"
                        " job_description, status, created_at, updated_at, saved_job_id)"
                        " VALUES (3, 1, 'Third', 'Microsoft', 'x', 'active', :now, :now, 1)"
                    ),
                    {"now": now},
                )
    finally:
        engine.dispose()


def test_many_goals_may_be_unlinked_at_once(migration_target):
    """The other half of the UNIQUE rule, and the one that would quietly cap the product at a
    single unlinked goal if the column had been made unique the naive way on a database that
    treats NULLs as equal."""
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_unlinked_rows(migration_target)
    command.upgrade(config, _THIS_REVISION)

    engine = db_safety.make_engine(migration_target)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    try:
        with engine.begin() as connection:
            for goal_id in (2, 3, 4):
                connection.execute(
                    text(
                        "INSERT INTO career_goals (id, profile_id, title, company,"
                        " job_description, status, created_at, updated_at)"
                        f" VALUES ({goal_id}, 1, 'Goal', 'Microsoft', 'x', 'active', :now, :now)"
                    ),
                    {"now": now},
                )
            assert connection.execute(text("SELECT COUNT(*) FROM career_goals")).scalar() == 4
    finally:
        engine.dispose()


def test_deleting_the_application_nulls_the_link_instead_of_raising(migration_target):
    """ON DELETE SET NULL at the database level, which is what keeps job_hunt's hard delete and
    account deletion working against a linked goal."""
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_unlinked_rows(migration_target)
    command.upgrade(config, _THIS_REVISION)

    engine = db_safety.make_engine(migration_target)
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE career_goals SET saved_job_id = 1 WHERE id = 1"))
        with engine.begin() as connection:
            # SQLite enforces foreign keys only when asked; production PostgreSQL always does.
            connection.execute(text("PRAGMA foreign_keys=ON")) if engine.dialect.name == "sqlite" else None
            connection.execute(text("DELETE FROM saved_jobs WHERE id = 1"))
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT saved_job_id FROM career_goals WHERE id = 1")
            ).scalar() is None
            assert connection.execute(text("SELECT COUNT(*) FROM career_goals")).scalar() == 1
    finally:
        engine.dispose()


def test_the_migration_round_trips(migration_target):
    """A downgrade that leaves something behind fails only on the next upgrade -- in practice,
    during a rollback, which is the worst possible moment to discover it."""
    config = _alembic_config()
    command.upgrade(config, _THIS_REVISION)

    command.downgrade(config, _PREVIOUS_REVISION)
    assert "saved_job_id" not in _columns(migration_target)
    assert _current_revision(migration_target) == _PREVIOUS_REVISION

    command.upgrade(config, _THIS_REVISION)
    assert "saved_job_id" in _columns(migration_target)
    assert _current_revision(migration_target) == _THIS_REVISION


def test_downgrading_keeps_every_goal_and_every_application(migration_target):
    """Rolling back discards links, which is unavoidable, but must not discard rows."""
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed_unlinked_rows(migration_target)
    command.upgrade(config, _THIS_REVISION)

    engine = db_safety.make_engine(migration_target)
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE career_goals SET saved_job_id = 1 WHERE id = 1"))
    finally:
        engine.dispose()

    command.downgrade(config, _PREVIOUS_REVISION)

    engine = db_safety.make_engine(migration_target)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM career_goals")).scalar() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM saved_jobs")).scalar() == 1
            assert connection.execute(
                text("SELECT title FROM career_goals WHERE id = 1")
            ).scalar() == "Data Engineer"
    finally:
        engine.dispose()
