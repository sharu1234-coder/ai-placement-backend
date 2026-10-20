"""The only sanctioned way for the test suite to learn its PostgreSQL URL.

The suite is destructive by construction: every PostgreSQL run begins with
``Base.metadata.drop_all``, and the migration tests go further and run ``alembic downgrade
base``. That is correct for a database that exists to be thrown away, and catastrophic for one
that does not -- and before this module the *only* thing standing between the two was a
comment in ``tests/conftest.py`` showing an example URL. A single mistyped environment
variable dropped whatever it named, before the first test ran, with no prompt and no warning.

So the URL is no longer read from the environment by the modules that use it. They call
:func:`resolve_test_database_url`, which returns the URL only if every check below passes and raises
:class:`UnsafeTestDatabase` otherwise. Making the guard the *supply route* rather than an
extra call at each site is deliberate: a new PostgreSQL test added later gets the protection
by the ordinary act of asking for the URL, and a test that bypasses the guard has to reach for
``os.environ`` explicitly, which is visible in review.

What this is and is not:

- It is a guard against *accident* -- a typo, a stale shell, a copied command line, a
  production URL left in the environment from a debugging session.
- It is **not** isolation. A test role that can reach production can still be made to destroy
  production by someone who means to. Isolation is a database-side property (§ 5 of the
  owner's requirements: a dedicated role with no production grants), and no amount of
  client-side parsing substitutes for it. :func:`resolve_test_database_url` refuses the common
  superuser role names precisely so that the name suffix is not the only thing being trusted,
  but a correct deployment still needs the role.

Credentials never appear in anything raised or logged here: the password is never read, and
every message is built from :func:`redact`, which reconstructs the URL from its host, port and
database only. A URL that fails to parse is not echoed at all -- not even through the parser's
own exception, whose message embeds the string it was given.
"""

from __future__ import annotations

import os
from typing import Final, Iterable, Optional
from urllib.parse import quote

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from app.database.base import Base

#: The environment variable naming the disposable PostgreSQL database.
URL_VAR: Final = "TEST_DATABASE_URL"

#: Requirement 1: an explicit, test-only opt-in. A URL alone is not consent -- the variable
#: can be inherited from a parent shell, a ``.env`` loaded for an unrelated reason or a CI
#: template. This one has no meaning outside the test suite, so its presence is a statement.
OPT_IN_VAR: Final = "TEST_DATABASE_DESTRUCTIVE"

#: Requirement 5's escape hatch, for an ephemeral container whose superuser *is* the test
#: role (GitHub Actions' ``services: postgres`` has exactly one). Setting it in a
#: long-lived environment defeats the point of the check it disables.
ALLOW_ADMIN_ROLE_VAR: Final = "TEST_DATABASE_ALLOW_ADMIN_ROLE"

_TRUE: Final = frozenset({"1", "true", "yes", "on"})

#: Roles that in practice own every database on the instance. Not an exhaustive list of
#: dangerous roles -- no such list exists -- but these are the ones a copied connection
#: string actually carries.
_ADMIN_ROLES: Final = frozenset({"postgres", "root", "admin", "sa", "superuser", "rdsadmin"})

#: Required suffix on the database name (requirement 3).
_REQUIRED_SUFFIX: Final = "_test"

#: Hosts that name this machine. Compared after normalisation so ``localhost`` and
#: ``127.0.0.1`` cannot be used to make a URL *look* different from the application's.
_LOOPBACK: Final = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})

_DEFAULT_PORTS: Final = {"postgresql": 5432, "mysql": 3306}

#: Alembic's own bookkeeping table is expected in a database the migration tests have touched,
#: so it is not evidence that the database belongs to something else.
_EXPECTED_EXTRA_TABLES: Final = frozenset({"alembic_version"})


class UnsafeTestDatabase(RuntimeError):
    """Raised instead of destroying a database that has not been proven disposable.

    Deliberately not a ``pytest.skip``: a misconfigured destructive target is a fault to fix,
    not a condition to tiptoe around, and a skip would let a mistyped URL pass unnoticed as
    "PostgreSQL not configured".
    """


def _is_true(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in _TRUE


def redact(url: URL) -> str:
    """The URL with every credential removed, built up rather than stripped down.

    ``URL.render_as_string(hide_password=True)`` masks the password but keeps the username,
    and a username is half a credential. This reconstructs the identity fields only, so there
    is nothing to leak even if the result is logged.
    """
    port = f":{url.port}" if url.port else ""
    return f"{url.get_backend_name()}://<redacted>@{url.host or '?'}{port}/{url.database or '?'}"


def _identity(url: URL) -> tuple[str, str, int, str]:
    """The fields that decide whether two URLs name the same database.

    Normalised so that superficial differences cannot smuggle a match past the equality check
    in :func:`check`: the driver is dropped (``postgresql://`` and ``postgresql+psycopg2://``
    are the same server), the host is lower-cased with loopback spellings collapsed, an
    omitted port becomes the backend default, and the query string is ignored.

    The database name is compared case-insensitively even though PostgreSQL treats it as
    case-sensitive. That errs toward refusing a database that might be different, which is
    the only direction worth erring in here.
    """
    host = (url.host or "").strip().lower()
    if host in _LOOPBACK:
        host = "localhost"
    backend = url.get_backend_name()
    port = url.port or _DEFAULT_PORTS.get(backend, 0)
    return (backend, host, port, (url.database or "").strip().casefold())


def check(raw: Optional[str], *, app_database_url: str, environ: Optional[dict] = None) -> Optional[URL]:
    """Validate a candidate test URL. Pure: parses and compares, never connects.

    Returns the parsed URL when it is safe to destroy, ``None`` when no PostgreSQL test
    database is configured at all (the SQLite default, which destroys nothing), and raises
    :class:`UnsafeTestDatabase` for anything in between -- missing opt-in, unparseable,
    non-PostgreSQL, nameless, wrongly named, admin-roled, or pointing at the application's
    own database.

    ``None`` for absent is what keeps the ordinary run working: the suite has always meant
    "no URL set" as "use in-memory SQLite", and turning that into an error would make the
    guard the thing that broke the suite.
    """
    env = os.environ if environ is None else environ

    if raw is None or not raw.strip():
        return None
    raw = raw.strip()

    # Requirement 1, before anything else is even parsed.
    if not _is_true(env.get(OPT_IN_VAR)):
        raise UnsafeTestDatabase(
            f"{URL_VAR} is set, but {OPT_IN_VAR} is not. The test suite drops every table in "
            f"that database before it runs, so it refuses to touch one it was not explicitly "
            f"told is disposable. Set {OPT_IN_VAR}=1 only for a database you are willing to "
            f"lose. (The URL is not shown here because it may contain credentials.)"
        )

    # Requirement 4. The parser's own message embeds the string it was handed, so neither the
    # exception nor the raw value is passed through -- only the fact that it failed.
    #
    # ValueError as well as ArgumentError: an unbracketed IPv6 host
    # (`@::1:5432/db`) sends SQLAlchemy's parser into int() on `1:5432`, which raises
    # ValueError and would otherwise escape this guard uncaught -- carrying the URL, and
    # therefore the password, into the traceback.
    try:
        url = make_url(raw)
    except (ArgumentError, ValueError) as exc:
        raise UnsafeTestDatabase(
            f"{URL_VAR} is not a parseable database URL ({type(exc).__name__}). It is not "
            f"echoed here because it may contain credentials."
        ) from None

    backend = url.get_backend_name()
    if backend != "postgresql":
        # An SQLite TEST_DATABASE_URL would be worse than useless: every code path these
        # tests exist to exercise on PostgreSQL would silently run on SQLite and pass, which
        # is the exact false confidence the PostgreSQL gate is there to prevent. And a file
        # path that happens to be the development app.db would then be dropped.
        raise UnsafeTestDatabase(
            f"{URL_VAR} must name a PostgreSQL database; got backend {backend!r} "
            f"({redact(url)}). These tests make PostgreSQL-specific claims, so running them "
            f"against another engine would report a pass that proves nothing."
        )

    if not url.host or not url.host.strip():
        raise UnsafeTestDatabase(f"{URL_VAR} has no host, so what it points at is ambiguous ({redact(url)}).")

    database = (url.database or "").strip()
    if not database:
        raise UnsafeTestDatabase(
            f"{URL_VAR} names no database ({redact(url)}). Without one the driver connects to "
            f"the role's default database, which on a shared instance is whatever happens to "
            f"be there."
        )

    # Requirement 2. Done before the suffix check so that the most dangerous mistake -- the
    # application's own URL pasted in -- is reported as what it is, even if that database
    # happens to be called something ending in _test.
    app_identity: Optional[tuple[str, str, int, str]] = None
    try:
        app_identity = _identity(make_url(app_database_url))
    except (ArgumentError, ValueError):
        # The application's URL being unparseable is its own problem, surfaced at startup by
        # app.core.config. It must not stop this guard from applying the other checks.
        app_identity = None
    if app_identity is not None and app_identity == _identity(url):
        raise UnsafeTestDatabase(
            f"{URL_VAR} names the same database as the application's DATABASE_URL "
            f"({redact(url)}). The suite would drop every table in it. Differences in driver "
            f"name, host spelling, port default or query string do not make it a different "
            f"database."
        )

    # Requirement 3.
    if not database.casefold().endswith(_REQUIRED_SUFFIX):
        raise UnsafeTestDatabase(
            f"{URL_VAR}'s database name must end in {_REQUIRED_SUFFIX!r}; got {redact(url)}. "
            f"This is a naming convention, not a security boundary -- it is here to catch the "
            f"typo, not to replace the dedicated role."
        )

    # Requirement 5. The suffix convention above protects nothing if the role can reach every
    # other database on the instance, so a role that owns the server is refused by default.
    role = (url.username or "").strip()
    if not role:
        raise UnsafeTestDatabase(
            f"{URL_VAR} names no role ({redact(url)}). The connection would fall back to the "
            f"OS user, whose grants are not knowable from here."
        )
    if role.lower() in _ADMIN_ROLES and not _is_true(env.get(ALLOW_ADMIN_ROLE_VAR)):
        raise UnsafeTestDatabase(
            f"{URL_VAR} connects as an instance-wide administrative role ({redact(url)}). "
            f"Use a dedicated test role with grants on that database only, so the database "
            f"name is not the sole thing keeping the suite away from production. If this is "
            f"an ephemeral container whose superuser is the test role, set "
            f"{ALLOW_ADMIN_ROLE_VAR}=1."
        )

    return url


def resolve_test_database_url() -> Optional[str]:
    """The validated URL for this process, or ``None`` when the suite should use SQLite.

    Module-level callers get the checks at import time, which is before any fixture runs and
    therefore before anything can be dropped. ``pytest`` turns an exception here into a
    collection error naming this module, so a misconfiguration stops the run with a message
    instead of a casualty.
    """
    from app.core.config import settings

    url = check(os.environ.get(URL_VAR), app_database_url=settings.database_url)
    return url.render_as_string(hide_password=False) if url is not None else None


def unexpected_tables(table_names: Iterable[str]) -> list[str]:
    """Tables in the target database that this suite's models do not account for.

    Requirement 6. Their presence means the database holds something the suite did not
    create, which is the signature of pointing at the wrong one -- an application database,
    a shared staging database, someone else's schema. It is evidence, not isolation: an
    *empty* production database would pass this check happily, which is why it is the last
    gate rather than the only one.
    """
    known = set(Base.metadata.tables) | set(_EXPECTED_EXTRA_TABLES)
    return sorted(name for name in table_names if name not in known)


def drop_all_safely(engine, *, inspect_schema: Optional[bool] = None) -> None:
    """``Base.metadata.drop_all``, with requirement 7 enforced by construction.

    Every destructive call site in the suite goes through here, so the schema check cannot be
    forgotten at a new one, and the ordering is not a convention anyone has to remember: the
    inspection either passes or raises, and ``drop_all`` is unreachable in the second case.

    The inspection is skipped for SQLite by default because an SQLite test database is a
    temporary file or an in-memory handle created moments earlier by the test itself -- there
    is no shared instance to point at by mistake, and no third party's tables to find.
    """
    if inspect_schema is None:
        inspect_schema = engine.url.get_backend_name() == "postgresql"

    if inspect_schema:
        strays = unexpected_tables(inspect(engine).get_table_names())
        if strays:
            shown = ", ".join(strays[:10]) + (f", +{len(strays) - 10} more" if len(strays) > 10 else "")
            raise UnsafeTestDatabase(
                f"{redact(engine.url)} contains {len(strays)} table(s) this suite did not "
                f"create, so it is not a database the suite owns: {shown}. Nothing was "
                f"dropped. Point {URL_VAR} at a database used only by the tests."
            )

    Base.metadata.drop_all(bind=engine)


class HiddenUrl(str):
    """A connection URL that refuses to print itself.

    pytest's default long traceback prints the *arguments* of the failing test function
    (`ReprFuncArgs`), not only its locals, and `--showlocals` is not involved. A fixture that
    returns a connection URL therefore puts the password into every failure report for every
    test that uses it -- which is how a test-role credential reached a saved Stage F log.

    It is a `str` subclass, so `create_engine`, `make_url` and string formatting all keep
    working on it; only `repr` changes, and `repr` is what the reporters call. The value is
    still the real URL, so this hides it from diagnostics, not from the database driver.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - exercised through pytest's reporters
        return "'<database url redacted>'"


def hidden(url: str) -> HiddenUrl:
    """Wrap a credential-bearing URL so that printing it does not disclose the credential."""
    return HiddenUrl(url)


def secrets_to_scrub() -> list[str]:
    """Every string that must never appear in a test report, longest first.

    Used by the conftest report hook as a second layer: `HiddenUrl` fixes the values this
    suite hands around, while this catches text that a driver or a library composed itself.
    Returns nothing at all when no destructive URL is configured, which is the ordinary
    SQLite run.
    """
    raw = resolve_test_database_url()
    if not raw:
        return []
    found = {raw}
    try:
        parsed = make_url(raw)
    except (ArgumentError, ValueError):
        return sorted(found, key=len, reverse=True)
    password = parsed.password
    if password:
        found.add(password)
        found.add(quote(password, safe=""))
    return sorted((item for item in found if item), key=len, reverse=True)


_BUSY_MARKERS: Final = ("is being accessed by other users", "being accessed by other")


def _blocking_sessions(admin_engine, name: str) -> str:
    """Who else is on that database, named without any credential.

    Reported instead of escalating privileges: if a drop cannot proceed, the useful fact is
    which role owns the session holding it, not a way around the permission check. A
    non-superuser sees every row of pg_stat_activity, with other roles' statement text
    hidden -- role and backend type, which is what matters here, are visible.
    """
    try:
        with admin_engine.connect() as connection:
            rows = connection.execute(
                text(
                    # Identity and lifecycle metadata only. No query text: another role's is
                    # hidden from this role anyway, and our own is not worth logging.
                    "SELECT pid, coalesce(usename, '(no role)') AS role,"
                    " coalesce(application_name, '(unnamed)') AS app,"
                    " coalesce(backend_type, '(unknown)') AS kind,"
                    " coalesce(state, '(none)') AS state,"
                    " to_char(backend_start, 'HH24:MI:SS') AS started"
                    " FROM pg_stat_activity WHERE datname = :name"
                    " ORDER BY backend_start"
                ),
                {"name": name},
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the original failure
        return f"(the session list could not be read: {type(exc).__name__})"
    if not rows:
        return "(no sessions were listed, so the block had already cleared)"
    return "; ".join(
        f"pid {row.pid} {row.role} / {row.kind} / app {row.app} / {row.state} / since {row.started}"
        for row in rows
    )


def drop_temp_database(admin_engine, name: str, *, attempts: int = 5, pause: float = 0.4) -> None:
    """Drop a temporary database this suite created, using only the privileges it has.

    Deliberately **not** `WITH (FORCE)`. FORCE makes the server terminate every backend on
    the target database and privilege-checks each one against the calling role -- including
    the superuser-owned *autovacuum worker* that may be vacuuming a database created seconds
    earlier. A least-privilege test role holds no privileges over the superuser, so FORCE
    fails with "permission denied to terminate process" precisely when autovacuum happens to
    be running there. That is why it failed exactly once across roughly seventy temporary
    databases in one run, rather than failing the same way every time: a connection this
    suite had leaked would be owned by the test role itself, and a role may always terminate
    its own backends.

    Plain `DROP DATABASE` asks the server to shut down autovacuum workers on that database
    itself, server-side, with no privilege check, and refuses only while genuine client
    sessions remain. It is the right instrument here, because this suite owns its temporary
    databases exclusively and has no other session to evict.

    The retry covers the moment between a test's last engine being disposed and the server
    finishing with the connection. It is bounded, and exhausting it raises with the owning
    roles named rather than reaching for a privilege.

    The admin engine is disposed either way, so a failed drop does not also leak the
    connection that attempted it.
    """
    import time

    last_error: Optional[Exception] = None
    try:
        for attempt in range(1, attempts + 1):
            try:
                with admin_engine.connect() as connection:
                    connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
                return
            except Exception as exc:  # noqa: BLE001 - re-raised below with diagnostics
                last_error = exc
                if not any(marker in str(exc) for marker in _BUSY_MARKERS):
                    raise
                if attempt < attempts:
                    time.sleep(pause)
        raise UnsafeTestDatabase(
            f"the temporary database {name} could not be dropped after {attempts} attempts "
            f"because sessions remained on it: {_blocking_sessions(admin_engine, name)}. "
            "Nothing was terminated and no privilege was escalated. Close the engines the "
            "test opened, or drop it by hand as the cluster's owner."
        ) from last_error
    finally:
        admin_engine.dispose()


APPLICATION_NAME: Final = "talviora-migration-tests"


def make_engine(url, **kwargs):
    """An engine for test code, with pooling off and the session labelled.

    Two deliberate differences from `create_engine`:

    **NullPool.** With the default `QueuePool`, `connection.close()` returns the connection
    to the pool *still open* -- the backend stays alive until the engine is disposed, or
    until the engine becomes unreachable and garbage collection happens to free the driver
    connection. Several of these modules built an engine inline, as
    `inspect(create_engine(url)).get_indexes(...)`, and never disposed it: the engine was
    unreferenced a statement later, so whether its backend was still there at teardown
    depended on when the collector ran. That is what left seven `talviora_test` client
    backends holding seven temporary databases open, and why the count moved between runs.
    With NullPool there is nothing to leak: `close()` closes the socket.

    **application_name.** A lingering session is then attributable from
    `pg_stat_activity` without reading anyone's statement text.

    Not a replacement for disposing an engine -- it removes the consequence of forgetting,
    which for a test suite that creates a database per test is the difference between a
    clean teardown and an undroppable database.
    """
    connect_args = dict(kwargs.pop("connect_args", {}))
    if str(url).startswith(("postgresql", "postgres")):
        connect_args.setdefault("application_name", APPLICATION_NAME)
    kwargs.setdefault("poolclass", NullPool)
    return create_engine(url, connect_args=connect_args, **kwargs)
