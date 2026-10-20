
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_db
from app.database.base import Base
from app.main import app
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.models.user import User  # noqa: F401  (registers the table with Base.metadata)
from tests import db_safety

# Opt-in: point the API suite at a real PostgreSQL instead of in-memory SQLite.
#
#   TEST_DATABASE_DESTRUCTIVE=1
#   TEST_DATABASE_URL=postgresql+psycopg2://talviora_test:...@127.0.0.1:55432/aiplacement_test
#   pytest
#
# Both variables, and a role that holds grants on that database only -- the fixture below
# drops every table in whatever the URL names, before every test, so tests/db_safety.py
# refuses a target that has not been declared disposable, is not PostgreSQL, is not named
# *_test, connects as an instance-wide administrator, or turns out on inspection to be the
# application's own database. That check runs at import, before this file's first drop.
#
# Unset -- which is every existing run, local and CI -- behaves exactly as before. The default
# matters: SQLite in-memory gives each test a private database for free and runs in seconds,
# and switching the whole suite to Postgres permanently would cost that for no gain on the
# tests that are really about pure logic.
#
# What it buys is the thing SQLite structurally cannot show us: production is PostgreSQL, and
# a transaction that fails mid-way behaves differently there (a failed statement poisons the
# whole transaction until rollback, where SQLite carries on). Savepoint-recovery code is
# written for that, so it deserves to be exercised against the database it was written for.
# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()


def _make_engine():
    """The test engine, SQLite by default and PostgreSQL when asked for."""
    if _TEST_DATABASE_URL:
        # No StaticPool and no check_same_thread: both are SQLite-specific. NullPool keeps each
        # test's connections from outliving it, which matters when the tables are dropped and
        # recreated between tests rather than vanishing with an in-memory database.
        #
        # lock_timeout is the difference between a suite that fails and a suite that hangs. A
        # session left open holds its locks, and the next test's DROP TABLE then waits behind
        # it with no timeout at all -- the run stops dead with no output and no failing test
        # name. Fifteen seconds is far longer than any statement here legitimately takes, so
        # anything that trips it is a real leak, and it says so instead of disappearing.
        return create_engine(
            _TEST_DATABASE_URL,
            poolclass=NullPool,
            connect_args={"options": "-c lock_timeout=15000"},
        )
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture()
def client():
    engine = _make_engine()
    # SQLite ignores foreign keys unless asked, per connection. Production is PostgreSQL,
    # which enforces them on every statement -- so without this the suite is structurally
    # unable to see a whole class of bug: deleting a row something still points at.
    #
    # It was hiding two, both of which produced a 500 in production and a pass here: deleting
    # a saved search that had an alert or a tracked job attached, and the placement-alarm
    # recovery path that deletes a challenge whose question-attempt rows were already
    # committed. Turning the pragma on was measured before being adopted -- those two were the
    # only failures across the whole suite, and both were real defects rather than fixture
    # artefacts.
    # PostgreSQL enforces foreign keys on every statement with nothing to turn on, so the
    # pragma is both unnecessary and invalid there.
    if not _TEST_DATABASE_URL:

        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # A real database persists between tests, so the previous test's rows have to go or the
    # seed below collides with itself. An in-memory SQLite database is discarded wholesale and
    # needs none of this.
    if _TEST_DATABASE_URL:
        db_safety.drop_all_safely(engine)
    Base.metadata.create_all(bind=engine)

    # Minimal role + skill-weight seed for tests (mirrors the real Alembic seed migrations,
    # not the full list -- see alembic/versions/*_seed_default_role_skill_weights.py).
    seed_session = testing_session_local()
    sde = Role(name="SDE", category="Engineering", description="Software Development Engineer")
    data_analyst = Role(name="Data Analyst", category="Data", description="Data Analyst")
    seed_session.add_all([sde, data_analyst])
    seed_session.commit()
    seed_session.refresh(sde)
    seed_session.add_all(
        [
            RoleSkillWeight(role_id=sde.id, skill="Aptitude", weight=0.5, assessment_type="aptitude"),
            RoleSkillWeight(role_id=sde.id, skill="DSA", weight=1.0, assessment_type="coding"),
            RoleSkillWeight(role_id=sde.id, skill="OOP", weight=0.7, assessment_type="technical"),
            RoleSkillWeight(role_id=sde.id, skill="DBMS", weight=0.7, assessment_type="sql"),
            RoleSkillWeight(role_id=sde.id, skill="OS", weight=0.5, assessment_type="technical"),
            RoleSkillWeight(role_id=sde.id, skill="Networks", weight=0.5, assessment_type="technical"),
        ]
    )
    seed_session.commit()
    seed_session.close()

    # Sessions handed out below, so they can be closed at teardown even when a test never
    # lets the generator finish. See the loop at the end of the fixture.
    issued_sessions = []

    def override_get_db():
        db = testing_session_local()
        issued_sessions.append(db)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # app is a single module-level object shared by every test in the session (imported
    # once), so the rate limiter's in-memory counters (app/core/rate_limit.py) would
    # otherwise leak across tests -- a rate-limit test consuming its quota could then cause
    # an unrelated later test hitting the same endpoint from the same client to fail.
    app.state.limiter.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    # Ten tests reach past the HTTP layer for a session with `next(app.dependency_overrides
    # [get_db]())`, which takes the yielded session and never resumes the generator -- so its
    # `finally: db.close()` never runs and the session outlives the test still inside an open
    # transaction. On SQLite that is invisible: every session shares one in-memory connection
    # and there is nothing to contend for. On PostgreSQL the connection stays checked out
    # holding the locks its last statement took, and the next test's `drop_all` waits behind
    # it -- which is exactly how the first full PostgreSQL run stopped dead, 215 tests in,
    # with `DROP TABLE interview_answers` blocked by an idle-in-transaction SELECT.
    #
    # Closing them here rather than editing those ten call sites keeps the assertions they
    # make untouched, and puts the cleanup where the ownership already is: this fixture
    # created every one of these sessions. `close()` on an already-closed session is a no-op,
    # so the sessions that did go through the generator cost nothing.
    for issued in issued_sessions:
        issued.close()
    engine.dispose()


# --------------------------------------------------------------------------- report scrubbing
# Second layer behind db_safety.HiddenUrl. pytest's default long traceback prints the failing
# function's arguments, and libraries compose their own messages, so a credential can reach a
# report from a direction this suite does not control. Anything the configured destructive URL
# would disclose is replaced here, at the last point before the text is displayed or written
# to a junit file.
#
# It is a no-op on an ordinary run: with no TEST_DATABASE_URL there is no secret to scrub, and
# the report object is left exactly as pytest built it.
_SCRUB = db_safety.secrets_to_scrub()


def _scrubbed(text: str) -> str:
    for secret in _SCRUB:
        text = text.replace(secret, "<redacted>")
    return text


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if _SCRUB and report.longrepr is not None:
        original = str(report.longrepr)
        cleaned = _scrubbed(original)
        if cleaned != original:
            # Replacing the structured longrepr with text costs the coloured source markers
            # and keeps every line of the traceback. That trade is deliberate: a readable
            # report that leaks a password is worse than a plain one that does not.
            report.longrepr = cleaned
    return report
