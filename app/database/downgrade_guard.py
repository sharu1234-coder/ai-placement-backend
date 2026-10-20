"""Refuse a migration downgrade that would destroy an outstanding obligation.

Written before the account-deletion outbox exists, deliberately. The outbox's `downgrade()`
drops the two tables that hold the *only* record of which objects a deleted user's erasure
still requires -- so a rollback performed during an incident, by someone who is not thinking
about erasure queues at that moment, silently converts "this user's data will be erased" into
"nobody knows it was ever meant to be". The guard is the reason that cannot happen quietly,
and testing it before the migration is written is the only way it gets tested at all: a guard
added in the same change as the table it protects tends to be verified by reading.

Every existing migration in this repository downgrades with plain, unconditional
`op.drop_index` / `op.drop_table`. This deviates from that convention on purpose and with the
owner's approval, for the one case where the dropped rows are an obligation to a person rather
than a cache of derived state.

**There is no environment-variable override.** An escape hatch is the thing that gets set in a
deploy pipeline and then forgotten, and "the rollback was blocked" is a recoverable situation
while "the erasure queue was dropped during a rollback" is not. If a downgrade genuinely has
to proceed, the operator resolves or parks the rows first -- a deliberate, auditable act in the
database, by someone who can see what they are discarding. That procedure is in
`docs/proposal-account-deletion-outbox.md`.

Imported from `app` rather than placed beside the migrations because `alembic/env.py` already
imports `app.core.config` and every model, so `app` is importable in migration context, and
because a helper that lives here can be unit-tested. It deliberately depends on no model and
no metadata -- only the connection it is handed -- so a future schema change cannot make an
old migration's guard fail to import.
"""

from __future__ import annotations

import re
from typing import Final, Sequence

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

#: Table and column names cannot be bound parameters, so they are interpolated -- and
#: therefore validated. Not because a migration author is hostile, but because a typo that
#: happens to contain a quote should fail as a bad identifier rather than as SQL.
_IDENTIFIER: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class OutstandingObligations(RuntimeError):
    """Raised instead of dropping rows that still represent work owed to a person."""


def _identifier(value: str, what: str) -> str:
    if not _IDENTIFIER.match(value or ""):
        raise ValueError(f"{what} is not a plain SQL identifier: {value!r}")
    return value


def refuse_if_outstanding(
    connection: Connection,
    *,
    table: str,
    unresolved: Sequence[str],
    obligation: str,
    status_column: str = "status",
) -> None:
    """Raise if `table` holds any row whose `status_column` is in `unresolved`.

    Returns quietly in the three cases where there is nothing to protect:

    - the table does not exist, which is what a downgrade run against a database that never
      had the revision looks like, and also what a re-run of a completed downgrade looks like;
    - the table is empty;
    - every row has reached a terminal status.

    The message carries the count and nothing else. No user id, no storage key, no row
    contents: this runs in migration output, which lands in deploy logs, and the whole point
    of the rows being protected is that they describe a person who asked to be forgotten.
    """
    _identifier(table, "table")
    _identifier(status_column, "status_column")
    if not unresolved:
        raise ValueError("unresolved must name at least one status, or the guard protects nothing")

    if not inspect(connection).has_table(table):
        return

    # Statuses are bound parameters -- expanding IN, so one placeholder per value.
    placeholders = ", ".join(f":s{i}" for i in range(len(unresolved)))
    params = {f"s{i}": value for i, value in enumerate(unresolved)}
    count = connection.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {status_column} IN ({placeholders})"),  # noqa: S608
        params,
    ).scalar_one()

    if not count:
        return

    raise OutstandingObligations(
        f"Refusing to downgrade: {table} holds {count} unresolved row(s) "
        f"({status_column} in {sorted(unresolved)!r}), each one {obligation}. Dropping the "
        f"table would destroy the only record of that obligation, and nothing downstream "
        f"would ever know it existed. Drain the queue first, or deliberately park these rows "
        f"in a terminal status, and run the downgrade again. There is no override flag; see "
        f"docs/proposal-account-deletion-outbox.md for the procedure."
    )
