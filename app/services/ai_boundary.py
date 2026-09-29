"""F12-07: the boundary between database transactions and slow AI/provider work.

The problem this exists to solve is not visible in any service's code, which is why it survived
so long. `SessionLocal` leaves SQLAlchemy's `autobegin` at its default, so a transaction starts
on the *first* statement a session emits and lives until commit, rollback or close. The very
first statement of every authenticated request is `get_current_user`'s read of the users row, on
the same Session FastAPI injects into the route body. So by the time any service runs, a
transaction is already open -- no service has to do anything wrong for an AI call to end up
inside one, and nothing anywhere says `begin()`.

Measured at the seam on real routes (tests/api/test_ai_transaction_boundary.py), before this
module existed: a transaction was open at every one of nine AI-calling routes, and it stayed open
for the whole call -- its age grew from 2.5ms to 402.7ms across a provider that slept 400ms. With
a real model that is 98-183s (Phase 17's measured range) of transaction lifetime per request.

What that costs, precisely, because the severity matters:

  * A pooled connection is pinned for the model's entire duration. The pool is 10 + 20 overflow,
    so roughly thirty concurrent generations exhaust it and *unrelated* cheap routes then block
    waiting for a connection.
  * On PostgreSQL the transaction's snapshot is retained for that whole time, which is what
    holds back dead-tuple reclamation and shows up as `idle in transaction`.
  * On SQLite pysqlite only emits BEGIN before a write, so a read-only phase is largely benign
    there -- this is a PostgreSQL-shaped problem, and production is PostgreSQL.

What it does NOT cost, and this was checked rather than assumed: no row locks and no SQLite
write lock are held across any AI call in this codebase. Every AI call runs in a read-only
transaction; the probe reports `wrote: False` at all fourteen call sites. Phase 15 had already
kept writes out of the window for Career Path, and the same turned out to hold everywhere.

So the fix is to end the read transaction before the provider call and reopen afterwards:

    SHORT DB READ  ->  CLOSE  ->  AI WORK  ->  REOPEN  ->  REVALIDATE  ->  ATOMIC WRITE  ->  COMMIT

`db.close()` is deliberate, and deliberately loud. It ends the transaction, returns the
connection to the pool, and detaches every ORM object, so a service that tries to keep using a
row it read *before* the AI call raises DetachedInstanceError instead of silently working from
state that may be minutes stale. The alternative -- ending the transaction while leaving loaded
values readable -- is quieter and worse: it makes exactly the stale-write bug this phase is meant
to prevent invisible, and an expired attribute would reopen a transaction mid-AI-call to refresh
itself, putting the transaction straight back where it was taken from.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# Sessions whose current transaction has emitted a write. Tracked because the session API cannot
# answer "has this transaction written anything" -- `new`/`dirty`/`deleted` only describe work that
# has not been flushed yet, and a flushed-but-uncommitted INSERT leaves all three empty while the
# row still exists only inside the transaction. Without this, closing the session at a boundary
# would silently roll that row back.
#
# This is not a theoretical gap. It is exactly how the professional-presence sync broke when this
# module was first written: the snapshot row was flushed but committed by the *caller* several lines
# later, so the guard below saw nothing pending, the close discarded the snapshot, and ten tests
# failed with an empty history. The listeners are module-level and keyed by id, and entries are
# removed when the transaction ends, so nothing accumulates.
_WROTE: set[int] = set()
_CONNECTION_TO_SESSION: dict[int, int] = {}


@event.listens_for(Session, "after_begin")
def _track_transaction(session, _transaction, connection):
    _WROTE.discard(id(session))
    if connection is not None:
        _CONNECTION_TO_SESSION[id(connection)] = id(session)


def _forget(session):
    _WROTE.discard(id(session))


event.listen(Session, "after_commit", _forget)
event.listen(Session, "after_rollback", _forget)


@event.listens_for(Engine, "before_cursor_execute")
def _note_write(conn, _cursor, statement, _params, _context, _executemany):
    if statement.lstrip()[:6].lower().startswith(("insert", "update", "delete")):
        session_id = _CONNECTION_TO_SESSION.get(id(conn))
        if session_id is not None:
            _WROTE.add(session_id)


class PendingWriteAtAIBoundary(RuntimeError):
    """Raised when a flow reaches an AI call with unflushed changes in the session.

    A programming error rather than a runtime condition, and it must not be softened. Ending the
    transaction would discard those objects silently, and committing them would split a write the
    product means to be atomic into a half-applied one -- so a flow in this position has to be
    reordered to do its AI work before it starts writing, which is what every flow here now does.
    """


@contextmanager
def outside_transaction(db: Session) -> Iterator[None]:
    """Guarantee that no database transaction is held while the body runs.

    Use it around every provider call that a request's session is anywhere near:

        with outside_transaction(db):
            parsed = ai_provider.generate_structured(prompt, Schema)

    After the body, the next statement opens a fresh transaction by itself. Anything the flow
    needs from the database must therefore be re-read on that side of the boundary -- which is
    the point, not an inconvenience: it is the only place a concurrent change can be noticed.
    """
    unflushed = len(db.new) + len(db.dirty) + len(db.deleted)
    flushed = id(db) in _WROTE and db.in_transaction()
    if unflushed or flushed:
        raise PendingWriteAtAIBoundary(
            f"uncommitted writes in the session at an AI boundary "
            f"({unflushed} unflushed object(s)"
            f"{', plus statements already flushed in this transaction' if flushed else ''}). "
            "Closing the transaction would discard them and committing them here would split a "
            "write the product means to be atomic -- do the AI work before writing, or commit the "
            "unit of work first."
        )
    # Ends the transaction, releases the pooled connection, and detaches every loaded object.
    db.close()
    yield
