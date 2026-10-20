"""The guard that stands between `pytest` and a database it must not drop.

Every PostgreSQL run in this suite begins by dropping every table, so the thing being tested
here is the only reason that is survivable. These tests use mocked configuration throughout --
they assert what the guard *refuses*, which needs no database and must not have one, since a
test that needed a real connection to prove the refusal would be unrunnable exactly when the
refusal matters most.

What is deliberately not claimed: none of this is PostgreSQL integration coverage. The lease
contention, concurrent-worker and crash-recovery tests the deletion outbox needs still require
an authorised disposable database and have not run.
"""

import pytest
from sqlalchemy import create_engine, inspect, text

from app.database.base import Base
from tests import db_safety
from tests.db_safety import UnsafeTestDatabase

# A realistic application URL, used as the thing the test URL must never turn out to be.
APP_URL = "postgresql+psycopg2://app_user:pr0d-pa55word@db.internal.example:5432/talviora"

# Credentials that must never appear in a message. Distinctive on purpose: a substring search
# for them is only meaningful if they could not occur by chance.
SECRET = "pr0d-pa55word"
ROLE_SECRET = "app_user"

SAFE = "postgresql+psycopg2://talviora_test:t3st-pa55word@127.0.0.1:55432/talviora_test"

OPT_IN = {db_safety.OPT_IN_VAR: "1"}


def reject(raw, env=None, app_url=APP_URL):
    """Call the guard expecting a refusal, and hand back the message for inspection."""
    with pytest.raises(UnsafeTestDatabase) as excinfo:
        db_safety.check(raw, app_database_url=app_url, environ=OPT_IN if env is None else env)
    return str(excinfo.value)


class TestMissingConfigurationIsTheSafeDefault:
    """No URL means SQLite in-memory, which has nothing to destroy."""

    @pytest.mark.parametrize("raw", [None, "", "   ", "\n"])
    def test_an_absent_url_resolves_to_none_rather_than_raising(self, raw):
        # Not an error: "unset" has always meant "use SQLite" here, and every existing run --
        # local and CI -- depends on it. A guard that broke the default run would be removed.
        assert db_safety.check(raw, app_database_url=APP_URL, environ={}) is None

    def test_the_opt_in_alone_does_not_conjure_a_database(self):
        assert db_safety.check(None, app_database_url=APP_URL, environ=OPT_IN) is None


class TestTheOptInIsRequired:
    def test_a_url_without_the_opt_in_is_refused(self):
        message = reject(SAFE, env={})
        assert db_safety.OPT_IN_VAR in message
        # A URL can be inherited from a parent shell or an unrelated .env; the opt-in cannot
        # be inherited by accident, because it means nothing outside this suite.
        assert "disposable" in message

    def test_the_refusal_does_not_echo_the_url(self):
        # Before parsing there is no safe way to redact it, so it is not shown at all.
        message = reject(SAFE, env={})
        assert "t3st-pa55word" not in message
        assert "talviora_test" not in message

    def test_a_falsey_opt_in_is_not_an_opt_in(self):
        for value in ["0", "false", "no", "", "maybe"]:
            assert db_safety.OPT_IN_VAR in reject(SAFE, env={db_safety.OPT_IN_VAR: value})

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
    def test_the_documented_spellings_all_work(self, value):
        assert db_safety.check(SAFE, app_database_url=APP_URL, environ={db_safety.OPT_IN_VAR: value})


class TestMalformedConfigurationFailsSafely:
    @pytest.mark.parametrize(
        "raw",
        [
            "not-a-url",
            "postgresql",
            "//host/db_test",
            "postgresql+psycopg2:/missing_slash_db_test",
            # An unbracketed IPv6 host: SQLAlchemy's parser reaches int("1:5432") and raises
            # ValueError, not ArgumentError. Caught here because an uncaught one would carry
            # the whole URL -- password included -- into the traceback.
            "postgresql+psycopg2://u:pw@::1:5432/db_test",
        ],
    )
    def test_an_unparseable_or_incomplete_url_is_refused(self, raw):
        # Each of these fails somewhere different -- the parser, the backend check, the host
        # check -- and all of them must end in a refusal rather than a connection attempt.
        assert db_safety.URL_VAR in reject(raw)

    def test_a_parse_failure_does_not_leak_the_string_it_failed_on(self):
        # SQLAlchemy's own ArgumentError embeds the URL it was handed ("Could not parse
        # SQLAlchemy URL from string '...'"), so the exception message is not passed through.
        # If it ever were, a password in a mistyped URL would land in the CI log.
        message = reject("postgres!!//" + SECRET + "@host/db_test")
        assert SECRET not in message

    def test_a_non_postgresql_backend_is_refused(self):
        # An SQLite TEST_DATABASE_URL is the quiet failure mode: every PostgreSQL-specific
        # claim in this suite would run against SQLite and pass, proving nothing -- and if the
        # path happened to be the development app.db, the suite would drop it.
        message = reject("sqlite:///./app.db")
        assert "PostgreSQL" in message

    def test_a_url_with_no_database_is_refused(self):
        # Without a database name the driver connects to the role's default, which on a shared
        # instance is whatever happens to be there.
        assert "names no database" in reject("postgresql+psycopg2://talviora_test:pw@127.0.0.1:55432/")

    def test_a_url_with_no_host_is_refused(self):
        assert "no host" in reject("postgresql+psycopg2:///talviora_test")

    def test_a_url_with_no_role_is_refused(self):
        # The connection would fall back to the OS user, whose grants are not knowable here.
        assert "names no role" in reject("postgresql+psycopg2://127.0.0.1:55432/talviora_test")


class TestTheApplicationDatabaseIsRefused:
    """Requirement 2: the most dangerous input is the application's own URL, pasted in."""

    @pytest.mark.parametrize(
        "variant",
        [
            # Same database, spelled differently every way the URL grammar allows.
            "postgresql+psycopg2://app_user:pr0d-pa55word@db.internal.example:5432/talviora",
            "postgresql://app_user:pr0d-pa55word@db.internal.example:5432/talviora",
            "postgresql+psycopg2://app_user:pr0d-pa55word@DB.Internal.Example:5432/talviora",
            "postgresql+psycopg2://app_user:pr0d-pa55word@db.internal.example/talviora",
            "postgresql+psycopg2://app_user:pr0d-pa55word@db.internal.example:5432/TALVIORA",
            "postgresql+psycopg2://other_role:other-pw@db.internal.example:5432/talviora",
            "postgresql+psycopg2://app_user:pr0d-pa55word@db.internal.example:5432/talviora?sslmode=require",
        ],
    )
    def test_equality_survives_superficial_formatting_differences(self, variant):
        # The driver suffix, the host's letter case, an omitted default port, a different
        # role, a query string and the database name's case all change the string without
        # changing which database it opens.
        assert "same database as the application" in reject(variant)

    def test_loopback_spellings_do_not_disguise_a_match(self):
        local_app = "postgresql+psycopg2://app_user:pw@localhost:5432/talviora"
        for host in ["127.0.0.1", "localhost", "[::1]", "0.0.0.0"]:
            message = reject(f"postgresql+psycopg2://app_user:pw@{host}:5432/talviora", app_url=local_app)
            assert "same database as the application" in message

    def test_a_different_host_is_not_treated_as_a_match(self):
        # Over-rejecting would be safe but useless: a real isolated database often shares the
        # application's database name on a different instance.
        other = "postgresql+psycopg2://talviora_test:pw@127.0.0.1:55432/talviora_test"
        assert db_safety.check(other, app_database_url=APP_URL, environ=OPT_IN) is not None

    def test_equality_is_reported_even_when_the_name_looks_disposable(self):
        # Checked before the _test suffix rule, so an application database that happens to be
        # called *_test is reported as what it actually is.
        app = "postgresql+psycopg2://app_user:pw@db.internal.example:5432/staging_test"
        assert "same database as the application" in reject(app, app_url=app)

    def test_an_unparseable_application_url_does_not_disable_the_other_checks(self):
        # If DATABASE_URL is itself broken that is a startup problem, surfaced by
        # app.core.config -- it must not turn this guard off.
        assert "_test" in reject("postgresql+psycopg2://r:pw@127.0.0.1:55432/production", app_url="garbage")


class TestTheNamingConventionIsEnforced:
    @pytest.mark.parametrize("name", ["talviora", "production", "test_talviora", "talviora_testing", "aiplacement"])
    def test_a_database_not_named_test_is_refused(self, name):
        message = reject(f"postgresql+psycopg2://talviora_test:pw@127.0.0.1:55432/{name}")
        assert "_test" in message

    def test_the_suffix_is_presented_as_a_typo_catcher_not_a_boundary(self):
        # Requirement 5 exists because a name suffix protects nothing on its own, and the
        # message says so rather than implying the suite is isolated because of it.
        assert "not a security boundary" in reject("postgresql+psycopg2://r:pw@127.0.0.1:55432/production")

    @pytest.mark.parametrize("name", ["talviora_test", "aiplacement_test", "x_TEST"])
    def test_a_conventionally_named_database_passes_this_check(self, name):
        assert db_safety.check(
            f"postgresql+psycopg2://talviora_test:pw@127.0.0.1:55432/{name}",
            app_database_url=APP_URL,
            environ=OPT_IN,
        )


class TestAdministrativeRolesAreRefused:
    """Requirement 5: the suffix is not the only thing keeping the suite off production."""

    @pytest.mark.parametrize("role", ["postgres", "root", "admin", "sa", "POSTGRES", "rdsadmin"])
    def test_an_instance_wide_role_is_refused_by_default(self, role):
        message = reject(f"postgresql+psycopg2://{role}:pw@127.0.0.1:55432/talviora_test")
        assert "administrative role" in message
        # And it says what to do instead, including the one case where it is legitimate.
        assert db_safety.ALLOW_ADMIN_ROLE_VAR in message

    def test_an_ephemeral_container_can_allow_it_explicitly(self):
        # GitHub Actions' `services: postgres` has exactly one role, and the whole instance
        # dies with the job. That is the only situation this hatch is for.
        env = {db_safety.OPT_IN_VAR: "1", db_safety.ALLOW_ADMIN_ROLE_VAR: "1"}
        assert db_safety.check(
            "postgresql+psycopg2://postgres:postgres@localhost:5432/aiplacement_test",
            app_database_url=APP_URL,
            environ=env,
        )

    def test_the_hatch_does_not_override_anything_else(self):
        # Requirement: explicit opt-in -- of either kind -- is not a master key.
        env = {db_safety.OPT_IN_VAR: "1", db_safety.ALLOW_ADMIN_ROLE_VAR: "1"}
        assert "_test" in reject("postgresql+psycopg2://postgres:pw@127.0.0.1:5432/production", env=env)
        assert "same database" in reject(APP_URL, env=env)
        assert "PostgreSQL" in reject("sqlite:///./app.db", env=env)


class TestAValidIsolatedConfigurationPasses:
    def test_the_documented_example_is_accepted(self):
        url = db_safety.check(SAFE, app_database_url=APP_URL, environ=OPT_IN)
        assert url is not None
        assert url.database == "talviora_test"
        assert url.port == 55432

    def test_the_password_survives_the_round_trip(self):
        # The guard redacts what it *reports*, not what it returns -- the suite still has to
        # be able to connect.
        url = db_safety.check(SAFE, app_database_url=APP_URL, environ=OPT_IN)
        assert url.password == "t3st-pa55word"


class TestCredentialsNeverReachAMessage:
    @pytest.mark.parametrize(
        "raw",
        [
            APP_URL,
            "postgresql+psycopg2://app_user:pr0d-pa55word@db.internal.example:5432/production",
            "postgresql+psycopg2://app_user:pr0d-pa55word@db.internal.example:5432/",
            "sqlite:///./app.db?password=pr0d-pa55word",
            "postgres!!//app_user:pr0d-pa55word@host/db_test",
        ],
    )
    def test_no_refusal_contains_the_password(self, raw):
        message = reject(raw)
        assert SECRET not in message

    def test_no_refusal_contains_the_role_either(self):
        # A username is half a credential, which is why redact() rebuilds the URL from its
        # identity fields instead of masking the password in the original.
        assert ROLE_SECRET not in reject(APP_URL)

    def test_a_refusal_still_says_which_database_it_refused(self):
        # Redaction that hid the host and database would make the error unactionable.
        message = reject("postgresql+psycopg2://app_user:pr0d-pa55word@db.internal.example:5432/production")
        assert "db.internal.example" in message and "production" in message

    def test_redact_rebuilds_the_url_without_credentials(self):
        from sqlalchemy.engine import make_url

        rendered = db_safety.redact(make_url(APP_URL))
        assert rendered == "postgresql://<redacted>@db.internal.example:5432/talviora"
        assert SECRET not in rendered and ROLE_SECRET not in rendered


class TestTheSchemaCheck:
    """Requirement 6: evidence that the database belongs to something else."""

    def test_the_suites_own_tables_are_not_strays(self):
        assert db_safety.unexpected_tables(list(Base.metadata.tables)) == []

    def test_alembics_bookkeeping_table_is_expected(self):
        # The migration tests leave it behind; finding it is not evidence of a wrong target.
        assert db_safety.unexpected_tables(["alembic_version"]) == []

    def test_a_foreign_table_is_reported(self):
        assert db_safety.unexpected_tables(["users", "payroll", "customers"]) == ["customers", "payroll"]


class TestNothingIsDroppedWhenTheGuardRefuses:
    """Requirement 7, demonstrated rather than asserted about.

    These use a real SQLite file so the drop is real: if the ordering were wrong, the table
    created below would be gone and the assertion would fail. `inspect_schema=True` forces the
    inspection that production code performs only for PostgreSQL, because an SQLite file
    created moments earlier by the test itself has no third party's tables to find.
    """

    def _engine(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
        Base.metadata.tables["users"].create(bind=engine)
        return engine

    def test_a_stray_table_stops_the_drop(self, tmp_path):
        engine = self._engine(tmp_path)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE payroll (id INTEGER PRIMARY KEY)"))

        with pytest.raises(UnsafeTestDatabase) as excinfo:
            db_safety.drop_all_safely(engine, inspect_schema=True)

        assert "payroll" in str(excinfo.value)
        assert "Nothing was\ndropped." in str(excinfo.value) or "Nothing was dropped." in str(excinfo.value)
        # The real proof: the table the suite owns is still there, so drop_all never ran.
        assert "users" in inspect(engine).get_table_names()
        assert "payroll" in inspect(engine).get_table_names()
        engine.dispose()

    def test_drop_all_is_never_reached_when_the_schema_is_foreign(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE payroll (id INTEGER PRIMARY KEY)"))

        calls = []
        monkeypatch.setattr(Base.metadata, "drop_all", lambda **kw: calls.append(kw))
        with pytest.raises(UnsafeTestDatabase):
            db_safety.drop_all_safely(engine, inspect_schema=True)
        assert calls == []
        engine.dispose()

    def test_a_clean_database_is_dropped_as_before(self, tmp_path):
        # The guard must not be so strict that the legitimate path stops working.
        engine = self._engine(tmp_path)
        db_safety.drop_all_safely(engine, inspect_schema=True)
        assert inspect(engine).get_table_names() == []
        engine.dispose()

    def test_sqlite_is_not_inspected_by_default(self, tmp_path):
        # Default behaviour is unchanged for the SQLite path: a temporary file the test just
        # created is dropped without ceremony, stray tables or not.
        engine = self._engine(tmp_path)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE payroll (id INTEGER PRIMARY KEY)"))
        db_safety.drop_all_safely(engine)
        assert inspect(engine).get_table_names() == ["payroll"]
        engine.dispose()


class TestTheGuardIsTheOnlySupplyRoute:
    """A test added later gets the protection by asking for the URL the ordinary way."""

    def test_no_test_module_reads_the_variable_directly(self):
        import pathlib
        import re

        offenders = []
        for path in pathlib.Path("tests").rglob("*.py"):
            if path.name == "db_safety.py":
                continue
            text_ = path.read_text(encoding="utf-8")
            if re.search(r"environ(\.get\(|\[)\s*[\"']TEST_DATABASE_URL", text_):
                offenders.append(str(path))
        assert offenders == [], f"these bypass tests/db_safety.py: {offenders}"

    def test_no_test_module_calls_drop_all_directly(self):
        import pathlib

        # Assembled, so this scanner does not report itself.
        needle = "metadata." + "drop_all("
        offenders = []
        for path in pathlib.Path("tests").rglob("*.py"):
            if path.name == "db_safety.py":
                continue
            if needle in path.read_text(encoding="utf-8"):
                offenders.append(str(path))
        assert offenders == [], f"these drop tables without the guard: {offenders}"

    def test_the_accessor_is_not_itself_collected_as_a_test(self):
        # A function named test_* imported into a test module is collected by pytest and
        # "passes" by doing the thing it was only meant to make available.
        assert not any(name.startswith("test_") for name in vars(db_safety) if callable(vars(db_safety)[name]))
