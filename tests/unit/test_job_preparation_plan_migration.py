"""The grounded-sections migration, run rather than read.

Same reasoning as test_professional_presence_migration.py: every other test builds its schema
with `Base.metadata.create_all`, which reads the models and would pass just as happily if this
migration were empty. Production is built by Alembic alone.

What this migration must get right is narrower than creating tables, and easier to get wrong
quietly: it adds three columns to a table that already holds rows. So the round trip is run
against a database that already contains a plan, and the existing row is checked afterwards --
an ALTER that dropped or rewrote existing data would still leave the columns looking correct.
"""

import datetime
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

_TABLE = "job_preparation_plans"
_NEW_COLUMNS = ("gap_guidance", "resume_recommendations", "project_connections")
_PREVIOUS_REVISION = "fb4bb77f3d17"
_THIS_REVISION = "e8691c85f409"


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    """A throwaway database this test owns -- it runs downgrades, which would wreck the
    suite's shared one. Mirrors the fixture in test_professional_presence_migration.py,
    including why a separate database rather than a search_path-scoped schema."""
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"prep_migration_test_{uuid.uuid4().hex[:8]}"
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
        url = f"sqlite:///{tmp_path / 'prep_migration.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
    """Programmatic, never Config("alembic.ini") -- that populates config_file_name, env.py
    then calls fileConfig(), and logging is reconfigured process-wide with
    disable_existing_loggers=True, silencing unrelated tests that assert on log output."""
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


def _insert_plan_at_previous_revision(url: str) -> int:
    """A plan written the way every pre-migration row was written: no new columns at all."""
    engine = db_safety.make_engine(url)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    try:
        with engine.begin() as connection:
            # Real owner rows: profile_id is NOT NULL on both tables below, and a plan with a
            # dangling owner would not be representative of anything in production.
            connection.execute(
                text(
                    "INSERT INTO users (email, password_hash, created_at, is_active, token_version) "
                    "VALUES ('migration@example.com', 'x', :now, :active, 0)"
                ),
                {"now": now, "active": True},
            )
            user_id = connection.execute(text("SELECT MAX(id) FROM users")).scalar()
            connection.execute(
                text("INSERT INTO student_profiles (user_id) VALUES (:user_id)"), {"user_id": user_id}
            )
            profile_id = connection.execute(text("SELECT MAX(id) FROM student_profiles")).scalar()
            connection.execute(
                text(
                    "INSERT INTO job_descriptions (profile_id, raw_text, source, created_at) "
                    "VALUES (:profile_id, 'jd text', 'paste', :now)"
                ),
                {"profile_id": profile_id, "now": now},
            )
            jd_id = connection.execute(text("SELECT MAX(id) FROM job_descriptions")).scalar()
            connection.execute(
                text(
                    "INSERT INTO job_preparation_plans "
                    "(jd_id, profile_id, job_summary, required_skills, preparation_gaps, "
                    " preparation_plan, interview_prep, created_at) "
                    "VALUES (:jd_id, :profile_id, :summary, '[]', '[]', '[]', '[]', :now)"
                ),
                {
                    "jd_id": jd_id,
                    "profile_id": profile_id,
                    "summary": '{"job_title": "Data Analyst"}',
                    "now": now,
                },
            )
            return connection.execute(text("SELECT MAX(id) FROM job_preparation_plans")).scalar()
    finally:
        engine.dispose()


def test_the_migration_round_trips_with_existing_data_intact(migration_target):
    config = _alembic_config()

    # Stop one revision short, write a row, then apply the migration on top -- which is what
    # applying this to a live database actually looks like.
    command.upgrade(config, _PREVIOUS_REVISION)
    plan_id = _insert_plan_at_previous_revision(migration_target)
    assert plan_id is not None

    # Pinned to this migration rather than "head", matching
    # test_professional_presence_migration.py. The two were equivalent when this test was
    # written because this migration WAS head -- which quietly made the test fail the moment
    # any later migration was added, for reasons having nothing to do with what it tests.
    command.upgrade(config, _THIS_REVISION)
    columns = _columns(migration_target)
    for name in _NEW_COLUMNS:
        assert name in columns, f"{name} missing after upgrade"
        assert columns[name]["nullable"] is True, f"{name} must be nullable for pre-existing rows"
    assert _current_revision(migration_target) == _THIS_REVISION

    # The pre-existing row survived, and its new columns are NULL rather than back-filled.
    engine = db_safety.make_engine(migration_target)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT job_summary, gap_guidance, resume_recommendations, project_connections "
                    "FROM job_preparation_plans WHERE id = :id"
                ),
                {"id": plan_id},
            ).fetchone()
        assert row is not None, "the migration lost an existing plan"
        assert "Data Analyst" in str(row[0]), "existing data was rewritten"
        assert row[1] is None and row[2] is None and row[3] is None
    finally:
        engine.dispose()

    command.downgrade(config, _PREVIOUS_REVISION)
    columns = _columns(migration_target)
    for name in _NEW_COLUMNS:
        assert name not in columns, f"{name} survived the downgrade"
    # The columns this migration did not add must still be there -- a downgrade that took the
    # rest of the table with it would also satisfy the assertion above.
    assert "job_summary" in columns
    assert "interview_prep" in columns
    assert _current_revision(migration_target) == _PREVIOUS_REVISION

    # Catches a downgrade that left something behind: it fails only on the next upgrade, which
    # in practice means during a rollback.
    command.upgrade(config, _THIS_REVISION)
    columns = _columns(migration_target)
    for name in _NEW_COLUMNS:
        assert name in columns, f"{name} missing after the second upgrade"
    assert _current_revision(migration_target) == _THIS_REVISION


def test_the_migration_produces_the_columns_the_models_declare(migration_target):
    """Guards against the migration and the model drifting apart, since everywhere else in the
    suite the schema comes from the model and the two are never compared."""
    from app.models.job_preparation_plan import JobPreparationPlan

    command.upgrade(config := _alembic_config(), "head")
    assert config is not None

    migrated = set(_columns(migration_target))
    declared = {c.name for c in JobPreparationPlan.__table__.columns}

    assert declared - migrated == set(), "model declares columns the migration does not create"
