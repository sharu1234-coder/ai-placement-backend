"""Connection parameters for the native PostgreSQL client tools.

libpq recognises a connection *URI* only when the string begins with ``postgresql://`` or
``postgres://``. A SQLAlchemy URL such as ``postgresql+psycopg2://...`` does not match, so
libpq treats the whole thing as a literal database *name* and connects to its own defaults --
which is how a backup aimed at an isolated cluster on port 55432 ended up knocking on
``localhost:5432`` and failing with "no password supplied".

Two further problems came with passing the URL straight through:

* it becomes an ``argv`` element, and a process's command line is readable by every process
  the same user owns;
* libpq echoes the string it could not use back in its own error text, so the credential
  reappears in logs and tracebacks.

So the URL is parsed once, here. Host, port, user and database are passed to the tools
explicitly, and the password is never an argument.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConnectionTargetError(RuntimeError):
    """A URL that cannot be turned into explicit client arguments.

    Its message never carries the URL or any part of a credential: the whole point of this
    module is that a parse failure must not be the thing that prints the password.
    """


@dataclass(frozen=True)
class Target:
    """Everything the native tools need, with the password kept out of the arguments."""

    host: str
    port: int
    user: str
    database: str
    password: Optional[str]

    def args(self) -> list[str]:
        """Explicit connection arguments. No URL, no credential."""
        return ["-h", self.host, "-p", str(self.port), "-U", self.user, "-d", self.database]

    def redacted(self) -> str:
        """Safe to log: identifies the endpoint without naming the user or the password."""
        return f"{self.host}:{self.port}/{self.database}"


def parse_target(url: str) -> Target:
    """Parse a SQLAlchemy or libpq URL into explicit, complete connection parameters.

    Host, port, user and database must all be present. A missing port is an error rather
    than a default, because libpq's default is 5432 and silently sending a destructive
    ``pg_restore`` to whatever answers on 5432 is the failure mode this module exists to
    prevent. The same reasoning applies to the host and the database name.
    """
    if not url:
        raise ConnectionTargetError("no database URL was supplied")
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):
        # Deliberately not chained and deliberately not quoting the exception: SQLAlchemy's
        # ArgumentError message embeds the URL it was handed, credentials included.
        raise ConnectionTargetError("the database URL could not be parsed") from None

    missing = [
        name
        for name, value in (
            ("host", parsed.host),
            ("port", parsed.port),
            ("user", parsed.username),
            ("database", parsed.database),
        )
        if not value
    ]
    if missing:
        raise ConnectionTargetError(
            "the database URL must state "
            + ", ".join(missing)
            + " explicitly; the native PostgreSQL tools would otherwise fall back to their "
            "own defaults, which for the port is 5432"
        )

    return Target(
        host=str(parsed.host),
        port=int(parsed.port),
        user=str(parsed.username),
        database=str(parsed.database),
        password=parsed.password,
    )


def child_env(target: Target, env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The environment for a client tool: an existing ``PGPASSFILE`` is left to do its job.

    Precedence is deliberate. libpq prefers ``PGPASSWORD`` over ``PGPASSFILE``, so setting
    the former would quietly override a password file that a caller had prepared on purpose
    (an isolated test harness, for instance, writes an ACL-restricted one). Only when there
    is no password file does the URL's own password get passed down -- through the
    environment, never through ``argv``.
    """
    result = dict(os.environ if env is None else env)
    if result.get("PGPASSFILE"):
        return result
    if target.password:
        result["PGPASSWORD"] = target.password
    return result


def redact_output(text: Optional[str], password: Optional[str]) -> str:
    """Remove a known password from tool output before it is logged or raised.

    A second line of defence, not the first: with explicit arguments libpq has no credential
    to echo. It matters because the first version of this code learned the hard way that an
    error message is a log sink like any other.
    """
    if not text:
        return ""
    if password:
        text = text.replace(password, "<redacted>")
    return text
