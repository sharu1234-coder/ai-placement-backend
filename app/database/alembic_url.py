"""Put a database URL into an Alembic ``Config`` without ConfigParser eating it.

Alembic's ``Config`` wraps ``configparser.ConfigParser``, which by default uses
``BasicInterpolation``: in a value, ``%`` introduces a substitution. A percent-encoded
credential is therefore invalid interpolation syntax -- ``%40`` reads as ``%4`` -- and
``set_main_option`` raises before any migration runs.

The raised ``ValueError`` embeds the value it rejected, so the credential lands in the
traceback. That is not a hypothetical: it is how a test-role password ended up in a saved
failure log. Both problems are fixed by escaping, which is why these two lines live in a
module with a name and a test rather than inline in ``env.py``.
"""

from __future__ import annotations

from typing import Any, Optional

SQLALCHEMY_URL = "sqlalchemy.url"


def escape_for_config(url: str) -> str:
    """Escape a value for a ConfigParser that interpolates.

    ``%`` doubles to ``%%``; reading the option back undoes it. Applied to the whole URL
    rather than to the credential alone, because percent-encoding is legal in any part of
    one -- a database name may carry it too.
    """
    return url.replace("%", "%%")


def redact(url: Optional[str]) -> str:
    """``scheme://<redacted>@host/path``, for messages. Never the credential."""
    if not url:
        return "<empty>"
    scheme, separator, rest = url.partition("://")
    if not separator:
        return "<unparseable url>"
    _, at, tail = rest.rpartition("@")
    if not at:
        return f"{scheme}://{rest}"
    return f"{scheme}://<redacted>@{tail}"


def set_sqlalchemy_url(config: Any, url: str) -> None:
    """Set ``sqlalchemy.url`` on an Alembic config and verify it survived the round trip.

    The verification is not ceremony. The escaping and the un-escaping happen in different
    places -- ours on write, ConfigParser's on read -- and a mismatch would hand Alembic a
    URL that is subtly not the one requested, which at migration time means connecting to
    the wrong database rather than failing. Checking equality here turns that into an error.
    """
    if not url:
        raise ValueError("no database URL was supplied for the Alembic configuration")

    try:
        config.set_main_option(SQLALCHEMY_URL, escape_for_config(url))
    except ValueError:
        # ConfigParser's own message quotes the rejected value. Not re-raised from it, and
        # not quoted: that message is where the credential leaked from.
        raise ValueError(
            f"the Alembic configuration rejected the database URL for {redact(url)}"
        ) from None

    if config.get_main_option(SQLALCHEMY_URL) != url:
        raise ValueError(
            f"the Alembic configuration did not preserve the database URL for {redact(url)}"
        )
