"""The migration itself, run rather than read.

`Base.metadata.create_all` builds the test schema everywhere else in this suite, which means
every other test would pass just as happily if the migration were empty, wrong, or missing --
create_all reads the models, not the migration. Production has no create_all anywhere in
`app/` and is built exclusively by Alembic, so without this file the two ways of producing the
schema are never compared and a defect in the migration reaches production unopposed.

So this runs the real thing: base -> this revision, back down, then up again. The
second upgrade is not redundant. A downgrade that drops a table but leaves an index, or leaves
a type behind, succeeds quietly and only fails on the *next* upgrade -- which in practice means
it fails during a rollback, at the worst possible moment.

Against SQLite by default and against PostgreSQL when TEST_DATABASE_URL is set. The partial
unique indexes are asserted from the database's own catalogue on PostgreSQL, because that is
the one place where "the index exists" and "the index has the WHERE clause that makes
reconnect-after-revoke possible" are genuinely different facts.
"""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import settings

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

_NEW_TABLES = (
    "provider_connections",
    "provider_oauth_states",
    "external_evidence_items",
    "external_profile_snapshots",
)

_PREVIOUS_REVISION = "e7c3a95d1b84"
_THIS_REVISION = "fb4bb77f3d17"


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    """A database this test owns outright.

    Never the suite's shared database: these tests run `downgrade`, which would drop tables
    other tests are relying on. On PostgreSQL that means a throwaway DATABASE per run.

    An earlier version of this fixture used a throwaway *schema* plus
    `?options=-csearch_path%3D<schema>` on the URL, which is a tidier idea and does not work
    here: Alembic's Config is a configparser, `%` starts an interpolation token, and the URL
    blew up with "invalid interpolation syntax" before a single migration ran. A separate
    database sidesteps both the escaping and the question of which schema unqualified DDL
    lands in.
    """
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"migration_test_{uuid.uuid4().hex[:8]}"
        # CREATE/DROP DATABASE cannot run inside a transaction block, hence AUTOCOMMIT against
        # the maintenance database rather than the one under test.
        admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        # render_as_string(hide_password=False), not str(): SQLAlchemy's URL.__str__ masks the
        # password as "***", which would be handed to Alembic as a literal and fail to connect.
        url = base.set(database=name).render_as_string(hide_password=False)
        monkeypatch.setattr(settings, "database_url", url)
        yield url
        with admin.connect() as connection:
            # FORCE (PostgreSQL 13+) terminates any connection the engines below left behind,
            # so a failed assertion cannot leave an undroppable database on the server.
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
    else:
        url = f"sqlite:///{tmp_path / 'migration.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
    """Built programmatically rather than from alembic.ini, and the difference matters.

    `Config("alembic.ini")` populates `config_file_name`, and alembic/env.py then calls
    `fileConfig()` on it. That is correct for the CLI and wrong inside a test process:
    fileConfig reconfigures Python's logging globally and, with logging.config's default
    `disable_existing_loggers=True`, silences every logger the ini does not name.

    That is not a local effect. Running this file first broke two unrelated subscription tests
    that assert on captured log output -- and only in the full-suite run, never when either file
    was run on its own, which is the most expensive way for a test to be wrong.

    Passing no file leaves `config_file_name` as None, so env.py skips fileConfig entirely, and
    the one thing this test genuinely needs from the ini -- where the migration scripts live --
    is set explicitly. Nothing in production changes; alembic.ini still configures the CLI.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


def _table_names(url: str) -> set:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _current_revision(url: str):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            return row[0] if row else None
    finally:
        engine.dispose()


def test_the_migration_round_trips_base_to_head_and_back(migration_target):
    config = _alembic_config()

    command.upgrade(config, _THIS_REVISION)
    tables = _table_names(migration_target)
    for name in _NEW_TABLES:
        assert name in tables, f"{name} missing after upgrade"
    assert _current_revision(migration_target) == _THIS_REVISION

    command.downgrade(config, _PREVIOUS_REVISION)
    tables = _table_names(migration_target)
    for name in _NEW_TABLES:
        assert name not in tables, f"{name} survived the downgrade"
    # The tables this migration did not create must still be there -- a downgrade that took
    # the rest of the schema with it would also satisfy the assertions above.
    assert "student_profiles" in tables
    assert _current_revision(migration_target) == _PREVIOUS_REVISION

    # The pass that catches a downgrade which dropped a table but left an index behind: that
    # failure is silent until something tries to create the table again.
    command.upgrade(config, _THIS_REVISION)
    tables = _table_names(migration_target)
    for name in _NEW_TABLES:
        assert name in tables, f"{name} missing after the second upgrade"
    assert _current_revision(migration_target) == _THIS_REVISION


def test_the_migration_produces_the_columns_the_models_declare(migration_target):
    """Guards against the migration and the models drifting apart. Everywhere else in the
    suite the schema comes from the models, so only this comparison can see a difference."""
    command.upgrade(_alembic_config(), _THIS_REVISION)

    engine = create_engine(migration_target)
    try:
        inspector = inspect(engine)
        connection_columns = {c["name"] for c in inspector.get_columns("provider_connections")}
        assert {
            "id", "profile_id", "provider", "provider_user_id", "provider_username",
            "access_token_encrypted", "refresh_token_encrypted", "token_expires_at",
            "granted_scopes", "status", "sync_status", "last_synced_at", "last_error_kind",
            "revoked_at", "created_at", "updated_at",
        } == connection_columns

        state_columns = {c["name"] for c in inspector.get_columns("provider_oauth_states")}
        assert {
            "id", "state_hash", "profile_id", "provider", "code_verifier_encrypted",
            "expires_at", "consumed_at", "created_at",
        } == state_columns

        evidence_columns = {c["name"] for c in inspector.get_columns("external_evidence_items")}
        assert {
            "id", "connection_id", "skill", "signal_kind", "source_ref", "observed_at",
            "detail", "created_at",
        } == evidence_columns
        # Named explicitly because their absence is the design, not an oversight: GitHub
        # produces no score, and a column for one would let a fabricated number reach the
        # Evidence Engine's `verified` threshold. self_asserted would be False in every row.
        assert "score" not in evidence_columns
        assert "self_asserted" not in evidence_columns
        # Ownership comes from connection_id alone -- a duplicated profile_id would be a
        # second thing that can disagree with the first.
        assert "profile_id" not in evidence_columns

        snapshot_columns = {c["name"] for c in inspector.get_columns("external_profile_snapshots")}
        assert {
            "id", "connection_id", "sequence", "fingerprint", "payload", "created_at",
        } == snapshot_columns
    finally:
        engine.dispose()


def test_foreign_keys_point_where_the_models_say(migration_target):
    command.upgrade(_alembic_config(), _THIS_REVISION)

    engine = create_engine(migration_target)
    try:
        inspector = inspect(engine)

        def referred(table):
            return {fk["referred_table"] for fk in inspector.get_foreign_keys(table)}

        assert referred("provider_connections") == {"student_profiles"}
        assert referred("provider_oauth_states") == {"student_profiles"}
        assert referred("external_evidence_items") == {"provider_connections"}
        assert referred("external_profile_snapshots") == {"provider_connections"}
    finally:
        engine.dispose()


@pytest.mark.skipif(not _TEST_DATABASE_URL, reason="partial index definitions are PostgreSQL-specific")
def test_postgresql_creates_both_indexes_as_genuinely_partial(migration_target):
    """The assertion SQLite cannot make.

    `get_indexes()` reports a unique index on both dialects and would be satisfied by a FULL
    unique index -- which would compile, migrate, and then permanently lock out any student who
    ever disconnected GitHub. So this reads PostgreSQL's own rendered index definition and
    requires the WHERE clause to be present.
    """
    command.upgrade(_alembic_config(), _THIS_REVISION)

    engine = create_engine(migration_target)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename = 'provider_connections'"
                )
            ).fetchall()
        definitions = {name: definition for name, definition in rows}

        for index_name in (
            "uq_provider_connection_active_per_provider",
            "uq_provider_connection_active_identity",
        ):
            assert index_name in definitions, f"{index_name} was not created"
            definition = definitions[index_name]
            assert "UNIQUE INDEX" in definition, f"{index_name} is not unique"
            assert "revoked_at IS NULL" in definition, (
                f"{index_name} is not partial -- a full unique index here would permanently "
                f"prevent reconnecting after a disconnect"
            )
    finally:
        engine.dispose()
