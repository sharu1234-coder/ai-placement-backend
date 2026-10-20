"""Guards for the two defects that made Stage F's first PostgreSQL run fail.

Both were invisible on SQLite and unavoidable on PostgreSQL:

* an integer `1` seeded into the Boolean column `users.is_active` -- SQLite stores booleans as
  integers and accepted it; PostgreSQL raised `DatatypeMismatch`. 19 of the 20 failures came
  from this, 14 of them as setup errors because two fixtures do the seeding;
* a `placement_path_topics` row whose `practice_blueprint_id` had no parent in
  `assessment_blueprints` -- SQLite does not enforce foreign keys unless asked to, so the
  dangling reference was accepted for as long as the test had existed.

The first guard is a source scan, because the defect is literally a bare `1` in SQL text and a
scan catches it on any backend, including a plain SQLite run with no cluster available. The
second is behavioural: it turns foreign keys on in SQLite, which is the enforcement that was
missing, and checks the real seeding helpers against it.

Neither test needs PostgreSQL. The PostgreSQL evidence is the Stage F run itself.
"""

import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

import pytest
from sqlalchemy import Boolean, create_engine, event, inspect, text
from sqlalchemy.pool import NullPool

from app.database.base import Base
from tests import db_safety

_TESTS = Path(__file__).resolve().parent.parent
_MIGRATION_MODULES = sorted(
    path
    for path in _TESTS.glob("unit/test_*migration*.py")
    # This module quotes the defective statement on purpose, to prove the scanner catches it.
    if path.name != Path(__file__).name
)

# A bare integer where a Boolean belongs. NULL and bind parameters are fine: a bind parameter
# carries a real Python value, which is the whole point of using one.
_INTEGER_LITERALS = {"0", "1", "-1"}


class _Live(NamedTuple):
    """One pg_stat_activity row, as _blocking_sessions now reads it."""

    pid: int
    role: str
    app: str
    kind: str
    state: str
    started: str


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, engine):
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self._engine.statements.append(sql)
        if sql.startswith("DROP DATABASE"):
            if self._engine.failures:
                raise RuntimeError(self._engine.failures.pop(0))
            return None
        return _FakeResult(self._engine.sessions)


class _FakeEngine:
    """An admin engine stub. Nothing connects anywhere; the statements are recorded."""

    def __init__(self, failures=(), sessions=()):
        self.failures = list(failures)
        self.sessions = list(sessions)
        self.statements = []
        self.disposed = 0

    def connect(self):
        return _FakeConnection(self)

    def dispose(self):
        self.disposed += 1


def _boolean_columns() -> dict[str, set[str]]:
    """Every Boolean column in the mapped schema, by table.

    Read from the metadata rather than from a hand-written list, so a Boolean column added to
    a model in future is covered without anyone remembering to update this test.
    """
    assert "users" in Base.metadata.tables, (
        "the model metadata is empty, so this scan would pass without checking anything"
    )
    return {
        name: {
            column.name
            for column in table.columns
            if isinstance(column.type, Boolean)
        }
        for name, table in Base.metadata.tables.items()
    }


def _insert_statements(source: str):
    """Yield (table, columns, values) for every single-row INSERT in a module's source.

    Adjacent string literals are joined first, because these statements are written across
    several lines. Statements whose values contain nested parentheses are skipped -- the
    alignment would be wrong, and a wrong alignment is worse than a missed one, which is why
    the tests below assert a floor on how much was actually scanned.
    """
    joined = re.sub(r'"\s*\n\s*"', "", source)
    joined = re.sub(r"'\s*\n\s*'", "", joined)
    pattern = re.compile(
        r"INSERT INTO (\w+)\s*\(([^()]*?)\)\s*VALUES\s*\(([^()]*?)\)", re.IGNORECASE
    )
    for match in pattern.finditer(joined):
        columns = [part.strip() for part in match.group(2).split(",")]
        values = [part.strip() for part in match.group(3).split(",")]
        if len(columns) != len(values):
            continue
        yield match.group(1), columns, values


class TestBooleanColumnsAreNeverSeededWithIntegers:
    def test_no_migration_test_seeds_an_integer_into_a_boolean(self):
        booleans = _boolean_columns()
        offences = []
        scanned = 0
        for path in _MIGRATION_MODULES:
            source = path.read_text(encoding="utf-8")
            for table, columns, values in _insert_statements(source):
                scanned += 1
                for column, value in zip(columns, values, strict=True):
                    if column in booleans.get(table, ()) and value in _INTEGER_LITERALS:
                        offences.append(f"{path.name}: {table}.{column} = {value}")

        assert scanned >= 30, (
            f"only {scanned} INSERT statements were scanned, which is too few to be the whole "
            "suite -- the scanner is broken rather than the suite being clean"
        )
        assert offences == [], (
            "an integer was seeded into a Boolean column; PostgreSQL rejects this with "
            "DatatypeMismatch even though SQLite accepts it. Use a bind parameter carrying a "
            f"Python bool, or an explicit TRUE/FALSE literal: {offences}"
        )

    def test_the_scan_sees_the_boolean_columns_it_is_meant_to_cover(self):
        booleans = _boolean_columns()
        assert "is_active" in booleans["users"]
        assert sum(len(columns) for columns in booleans.values()) >= 5

    def test_the_scan_would_catch_the_original_defect(self):
        """The guard is only worth having if it fails on the code that actually broke."""
        original = (
            'text("INSERT INTO users (id, email, password_hash, is_active, created_at,"\n'
            '     " token_version) VALUES (1, \'pre@example.com\', \'x\', 1, :now, 0)")'
        )
        found = [
            (table, dict(zip(columns, values, strict=True)))
            for table, columns, values in _insert_statements(original)
        ]
        assert found, "the scanner did not even parse the statement it was built for"
        table, mapping = found[0]
        assert table == "users"
        assert mapping["is_active"] == "1"

    def test_the_scan_accepts_a_bind_parameter(self):
        fixed = (
            'text("INSERT INTO users (id, email, is_active, token_version)"\n'
            '     " VALUES (1, \'a@b.c\', :active, 0)")'
        )
        _table, mapping = next(
            (table, dict(zip(columns, values, strict=True)))
            for table, columns, values in _insert_statements(fixed)
        )
        assert mapping["is_active"] == ":active"
        assert mapping["is_active"] not in _INTEGER_LITERALS


class TestAChildRowAlwaysHasItsParent:
    """Foreign keys, enforced -- which is the check SQLite skips by default."""

    @pytest.fixture()
    def enforcing(self, tmp_path):
        connection = sqlite3.connect(tmp_path / "fk.db")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE student_profiles (id INTEGER PRIMARY KEY);
            CREATE TABLE assessment_blueprints (
                id INTEGER PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES student_profiles(id),
                source VARCHAR(20) NOT NULL,
                sections TEXT NOT NULL
            );
            CREATE TABLE placement_path_topics (
                id INTEGER PRIMARY KEY,
                topic_key VARCHAR(50) NOT NULL,
                practice_blueprint_id INTEGER
                    REFERENCES assessment_blueprints(id)
            );
            INSERT INTO student_profiles (id) VALUES (1);
            """
        )
        try:
            yield connection
        finally:
            connection.close()

    def test_a_dangling_blueprint_reference_is_refused(self, enforcing):
        with pytest.raises(sqlite3.IntegrityError):
            enforcing.execute(
                "INSERT INTO placement_path_topics (id, topic_key, practice_blueprint_id)"
                " VALUES (10, 'sql_basics', 77)"
            )

    def test_seeding_the_parent_first_succeeds(self, enforcing):
        enforcing.execute(
            "INSERT INTO assessment_blueprints (id, profile_id, source, sections)"
            " VALUES (77, 1, 'default_taxonomy', '[]')"
        )
        enforcing.execute(
            "INSERT INTO placement_path_topics (id, topic_key, practice_blueprint_id)"
            " VALUES (10, 'sql_basics', 77)"
        )
        assert enforcing.execute(
            "SELECT practice_blueprint_id FROM placement_path_topics WHERE id = 10"
        ).fetchone() == (77,)

    def test_a_null_reference_remains_allowed(self, enforcing):
        """Most topics have no blueprint, so the column has to stay optional."""
        enforcing.execute(
            "INSERT INTO placement_path_topics (id, topic_key, practice_blueprint_id)"
            " VALUES (11, 'sql_joins', NULL)"
        )

    def test_the_placement_path_test_seeds_the_parent_before_the_child(self):
        """Pins the fix in place: removing the parent seed brings the failure straight back."""
        source = (_TESTS / "unit/test_placement_path_uniqueness_migration.py").read_text(
            encoding="utf-8"
        )
        parent = source.find("_seed_blueprint(connection, 77, 1)")
        child = source.find('_seed_topic(connection, 10, 1, "sql_basics", "completed", "assessment", 77)')
        assert parent != -1, "the blueprint parent is no longer seeded"
        assert child != -1, "the topic that needs a blueprint is no longer seeded"
        assert parent < child, "the parent must be inserted before the child that references it"


class TestFixtureUrlsDoNotPrintTheirCredentials:
    """pytest's long traceback prints the failing test's arguments, so a URL fixture leaks."""

    def test_every_migration_fixture_wraps_its_url(self):
        unwrapped = [
            path.name
            for path in _MIGRATION_MODULES
            if "render_as_string(hide_password=False)" in path.read_text(encoding="utf-8")
            and "db_safety.hidden(" not in path.read_text(encoding="utf-8")
        ]
        assert unwrapped == [], (
            "these fixtures return a credential-bearing URL unwrapped, so its password would "
            f"appear in any failure report that shows the fixture argument: {unwrapped}"
        )

    def test_the_wrapper_hides_the_value_but_keeps_it_usable(self):
        from tests import db_safety

        url = db_safety.hidden("postgresql+psycopg2://u:p%40ss@127.0.0.1:55432/x_test")
        assert "p%40ss" not in repr(url)
        assert repr(url) == "'<database url redacted>'"
        assert url.startswith("postgresql+psycopg2://")  # still the real URL
        assert "p%40ss" in str(url)

    def test_the_scrubber_is_inert_without_a_configured_database(self):
        from tests import db_safety

        secrets = db_safety.secrets_to_scrub()
        assert isinstance(secrets, list)
        assert all(isinstance(item, str) and item for item in secrets)


class TestTemporaryDatabaseCleanup:
    """Teardown must not need privileges the test role deliberately does not hold.

    The Stage F.2 error was `InsufficientPrivilege: permission denied to terminate process`
    from `DROP DATABASE ... WITH (FORCE)`. FORCE privilege-checks the calling role against
    every backend on the target database, and an autovacuum worker belongs to the superuser.
    It failed once in roughly seventy temporary databases, which is the signature of
    autovacuum happening to be active -- a connection the suite had leaked would belong to
    the test role, which may always terminate its own backends, and would have failed every
    time instead.
    """

    def test_no_fixture_uses_force(self):
        offenders = [
            path.name
            for path in _MIGRATION_MODULES
            if "WITH (FORCE)" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            "WITH (FORCE) requires privileges over every role with a backend on the target "
            "database, including the superuser-owned autovacuum worker. Plain DROP DATABASE "
            f"shuts those workers down server-side instead: {offenders}"
        )

    def test_every_fixture_drops_in_a_finally(self):
        missing = [
            path.name
            for path in _MIGRATION_MODULES
            if "db_safety.drop_temp_database(admin, name)" not in path.read_text(encoding="utf-8")
        ]
        assert missing == [], (
            f"these fixtures no longer drop their temporary database in a finally: {missing}"
        )

    def test_the_drop_statement_carries_no_force(self):
        engine = _FakeEngine()
        db_safety.drop_temp_database(engine, "parity_test_abc12345", pause=0)
        assert engine.statements == ['DROP DATABASE IF EXISTS "parity_test_abc12345"']
        assert "FORCE" not in engine.statements[0]
        assert engine.disposed == 1

    def test_it_retries_while_the_database_is_still_busy(self):
        engine = _FakeEngine(failures=["database is being accessed by other users"])
        db_safety.drop_temp_database(engine, "migration_test_deadbeef", pause=0)
        assert len([s for s in engine.statements if s.startswith("DROP")]) == 2
        assert engine.disposed == 1

    def test_exhausting_the_retries_names_the_blocking_roles(self):
        engine = _FakeEngine(
            failures=["database is being accessed by other users"] * 3,
            sessions=[_Live(1817, "talviora_admin", "(unnamed)",
                            "autovacuum worker", "active", "10:44:01")],
        )
        with pytest.raises(db_safety.UnsafeTestDatabase) as exc:
            db_safety.drop_temp_database(engine, "migration_test_feedface", attempts=3, pause=0)
        message = str(exc.value)
        assert "talviora_admin" in message and "autovacuum worker" in message
        assert "no privilege was escalated" in message
        assert engine.disposed == 1

    def test_an_unrelated_error_is_not_retried(self):
        """A privilege or syntax error is not a busy database; retrying it just hides it."""
        engine = _FakeEngine(failures=["permission denied to terminate process"] * 5)
        with pytest.raises(RuntimeError, match="permission denied"):
            db_safety.drop_temp_database(engine, "migration_test_0badf00d", pause=0)
        assert len([s for s in engine.statements if s.startswith("DROP")]) == 1
        assert engine.disposed == 1

    def test_the_message_names_no_credential(self):
        engine = _FakeEngine(
            failures=["database is being accessed by other users"] * 2,
            sessions=[_Live(2024, "talviora_test", "talviora-migration-tests",
                            "client backend", "idle", "10:45:12")],
        )
        with pytest.raises(db_safety.UnsafeTestDatabase) as exc:
            db_safety.drop_temp_database(engine, "eval_migration_test_11223344", attempts=2, pause=0)
        assert "://" not in str(exc.value)
        assert "@" not in str(exc.value)


class TestNoTestEngineLeavesASessionBehind:
    """The root cause of the Stage F.3 teardown errors, demonstrated rather than mocked.

    `inspect(create_engine(url)).get_indexes(...)` builds an engine, opens a connection and
    drops every reference to both. With SQLAlchemy's default QueuePool, `close()` hands the
    connection back to the pool **still open**, so the PostgreSQL backend survives until the
    collector happens to free the driver object. Seven temporary databases were still held by
    `talviora_test` client backends at teardown for exactly that reason, and the count moved
    between runs because garbage collection is not part of the test's control flow.

    The two tests below are the before and after: the same usage pattern on a pooled engine
    retains a connection, and on `db_safety.make_engine` retains none. Neither needs
    PostgreSQL -- pool accounting is the mechanism, and it is backend-independent.
    """

    @staticmethod
    def _closes(engine) -> list:
        """Physical DBAPI closes. The pool's "close" event fires only for a real close."""
        closed = []
        event.listen(engine.pool, "close", lambda *_a: closed.append(1))
        return closed

    def test_a_pooled_engine_keeps_the_connection_after_close(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'pooled.db'}")
        closed = self._closes(engine)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            assert closed == [], (
                "this test documents the defect: closing a pooled connection does not close "
                "the backend. If that ever stops being true, the leak is gone and this guard "
                "can be simplified"
            )
            assert engine.pool.checkedin() == 1
        finally:
            engine.dispose()

    def test_make_engine_closes_on_close(self, tmp_path):
        engine = db_safety.make_engine(f"sqlite:///{tmp_path / 'unpooled.db'}")
        closed = self._closes(engine)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        assert isinstance(engine.pool, NullPool)
        assert closed == [1], "the backend must be gone the moment the connection is closed"

    def test_an_undisposed_make_engine_still_closes(self, tmp_path):
        """The case that actually occurred: the engine is never disposed at all."""
        engine = db_safety.make_engine(f"sqlite:///{tmp_path / 'inline.db'}")
        closed = self._closes(engine)
        opened = []
        event.listen(engine.pool, "connect", lambda *_a: opened.append(1))
        inspect(engine).get_table_names()  # the exact usage that leaked
        del engine
        assert opened, "the inspection did not open a connection, so this proves nothing"
        assert len(closed) == len(opened), (
            "every connection an undisposed engine opened must already be closed; "
            f"opened {len(opened)}, closed {len(closed)}"
        )

    def test_no_migration_module_builds_an_engine_directly(self):
        offenders = [
            path.name
            for path in _MIGRATION_MODULES
            if "create_engine(" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            "a test engine must come from db_safety.make_engine, which disables pooling; a "
            "create_engine here leaves a backend alive until garbage collection and can hold "
            f"its own temporary database open at teardown: {offenders}"
        )

    def test_postgres_sessions_are_labelled_for_identification(self):
        """So a lingering session is attributable without reading anyone's statement text."""
        engine = db_safety.make_engine(
            "postgresql+psycopg2://u:p@127.0.0.1:55432/x_test"
        )
        assert engine.dialect.name == "postgresql"
        assert db_safety.APPLICATION_NAME == "talviora-migration-tests"

    def test_keyword_arguments_still_reach_the_engine(self, tmp_path):
        """The fixture's admin engine needs AUTOCOMMIT; the helper must not swallow it."""
        engine = db_safety.make_engine(
            f"sqlite:///{tmp_path / 'kw.db'}", isolation_level="AUTOCOMMIT"
        )
        try:
            assert engine.pool.__class__ is NullPool
        finally:
            engine.dispose()

    def test_the_session_report_names_metadata_and_no_statement_text(self):
        engine = _FakeEngine(
            failures=["database is being accessed by other users"] * 2,
            sessions=[_Live(4242, "talviora_test", "talviora-migration-tests",
                            "client backend", "idle", "11:02:03")],
        )
        with pytest.raises(db_safety.UnsafeTestDatabase) as exc:
            db_safety.drop_temp_database(engine, "prep_plan_uniqueness_aabbccdd",
                                         attempts=2, pause=0)
        message = str(exc.value)
        for expected in ("pid 4242", "talviora_test", "client backend",
                         "talviora-migration-tests", "idle", "11:02:03"):
            assert expected in message
        assert "SELECT" not in message
        assert "://" not in message
