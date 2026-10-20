"""F-10: the migrations and the models must describe the same schema, forever.

Parity was measured clean during the Stage 5 audit, so nothing here is fixing a present
defect -- it is closing the hole that let F-01 happen. That defect was not a logic error: a new
profile-owned table landed and the code that had to know about it did not. Schema drift has the
same shape. A model gains a column, nobody writes the migration, every test still passes
because the test database is built from the models rather than from the migrations, and it
fails for the first time in production -- where /health/ready cannot catch it either, because
the revision still matches.

These tests build the database the way a deployment does (alembic upgrade) and compare it
against the way the tests do (Base.metadata), which is the only place the two can be seen to
disagree.

Scope, stated rather than implied: compare_metadata reports tables, columns, types and
nullability. It does not diff server defaults or CHECK constraints, and index/constraint naming
differences are not treated as drift. Both backends were measured at zero diffs before this was
written, so no filtering is applied and none is hidden -- if a future change makes this noisy,
the right response is to narrow the assertion deliberately, not to widen the exemption.
"""

import os
import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, Table, inspect, text
from sqlalchemy.engine.url import make_url

from alembic import command
from app.core.config import settings
from app.database.base import Base

# Imported for the side effect the whole comparison rests on: pulling in every router pulls in
# every model, so Base.metadata is complete. Without it this file would compare the migrations
# against whichever handful of models happened to be imported, and pass while missing most of
# the schema.
import app.main  # noqa: F401  isort:skip
from tests import db_safety

# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()


def _alembic_config() -> Config:
    """Programmatic, never Config("alembic.ini").

    Passing a filename populates config_file_name, alembic/env.py then calls fileConfig(), and
    logging is reconfigured process-wide with disable_existing_loggers=True -- silencing
    unrelated tests that assert on log output. Same reasoning as the other migration tests.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    """A throwaway database this test owns, because it runs downgrades.

    The URL is injected by monkeypatching settings.database_url rather than by setting
    sqlalchemy.url on the Config: alembic/env.py overwrites that option with
    settings.database_url on every run, so a URL passed through the Config is silently ignored
    and the migrations execute against the developer's real database instead. Mirrors the
    fixture in test_job_preparation_plan_migration.py.
    """
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"parity_test_{uuid.uuid4().hex[:8]}"
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
        url = f"sqlite:///{tmp_path / 'parity.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _diffs_against_models(url: str) -> list:
    engine = db_safety.make_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()


def _table_names(url: str) -> set:
    engine = db_safety.make_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _describe(diffs) -> str:
    return "\n".join(f"  {str(d)[:200]}" for d in diffs)


def test_migrations_build_exactly_the_schema_the_models_declare(migration_target):
    """The guard itself: upgrade to head, then diff against Base.metadata.

    A failure here names the drift directly -- add_table/remove_table for a model whose
    migration was never written (or a migration whose model was deleted),
    add_column/remove_column for a field, and modify_nullable/modify_type for a changed one.
    The fix is a migration, never a change to this test.
    """
    command.upgrade(_alembic_config(), "head")

    diffs = _diffs_against_models(migration_target)

    assert diffs == [], (
        "The Alembic migrations and the SQLAlchemy models disagree about the schema. "
        "The tests build their database from the models, so this cannot fail there -- it "
        "fails in production, against a database built by the migrations:\n"
        + _describe(diffs)
    )


def test_the_parity_check_actually_detects_drift(migration_target):
    """Proves the assertion above can fail. A guard that only ever passes guards nothing.

    Drift is injected into a throwaway MetaData rather than into the real models or the real
    schema, so this cannot leave anything behind for another test to trip over.
    """
    command.upgrade(_alembic_config(), "head")

    drifted = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(drifted)
    Table("a_table_no_migration_ever_created", drifted, Column("id", Integer, primary_key=True))

    engine = db_safety.make_engine(migration_target)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diffs = compare_metadata(context, drifted)
    finally:
        engine.dispose()

    reported = {
        d[1].name
        for d in diffs
        if isinstance(d, tuple) and d[0] == "add_table" and hasattr(d[1], "name")
    }
    assert "a_table_no_migration_ever_created" in reported, (
        f"the parity check missed an entire missing table, so it would miss a missing column "
        f"too; it reported: {_describe(diffs)}"
    )


def test_the_whole_migration_chain_downgrades_and_re_upgrades(migration_target):
    """Every revision's downgrade, not just the newest one's.

    Before this, two of the thirty-seven revisions had upgrade/downgrade/re-upgrade coverage --
    the two whose authors wrote it at the time. A downgrade is only ever run in an emergency,
    which is the worst moment to discover that one of them drops a table but leaves its index,
    or leaves a column behind so the next upgrade fails on a duplicate.

    Running the full chain to base and back exercises all of them at once, and stays correct as
    revisions are added, which a per-revision test does not.
    """
    config = _alembic_config()

    command.upgrade(config, "head")
    after_upgrade = _table_names(migration_target)
    assert len(after_upgrade) > 1, "upgrade head created nothing -- the chain did not run"

    command.downgrade(config, "base")
    after_downgrade = _table_names(migration_target)
    leftovers = after_downgrade - {"alembic_version"}
    assert not leftovers, (
        "downgrading to base left tables behind, so some revision's downgrade is incomplete: "
        + ", ".join(sorted(leftovers))
    )

    command.upgrade(config, "head")
    after_reupgrade = _table_names(migration_target)
    assert after_reupgrade == after_upgrade, (
        "re-upgrading after a full downgrade did not reproduce the same schema. Missing: "
        f"{sorted(after_upgrade - after_reupgrade)}; unexpected: "
        f"{sorted(after_reupgrade - after_upgrade)}"
    )


def test_the_schema_still_matches_the_models_after_a_downgrade_round_trip(migration_target):
    """The two guarantees above, composed.

    A chain can round-trip on table names alone while a downgrade quietly drops a column that
    its upgrade then recreates with a different type or nullability. Comparing against the
    models after the round trip is what catches that.
    """
    config = _alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    diffs = _diffs_against_models(migration_target)

    assert diffs == [], (
        "The schema no longer matches the models after downgrade-to-base and re-upgrade, so a "
        "revision's downgrade and upgrade are not inverses:\n" + _describe(diffs)
    )


def test_there_is_exactly_one_migration_head():
    """Two heads mean two people branched the chain, and `alembic upgrade head` then applies
    only one of them -- leaving a deployment with half the schema and no error to say so."""
    script = ScriptDirectory.from_config(_alembic_config())

    heads = script.get_heads()

    assert len(heads) == 1, (
        f"expected a single migration head, found {len(heads)}: {heads}. "
        "Merge them with `alembic merge` before this reaches a deployment."
    )
