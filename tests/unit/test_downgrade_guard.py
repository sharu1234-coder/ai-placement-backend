"""The migration downgrade guard, tested before the outbox it will protect exists.

That ordering is the point. A guard written in the same change as its migration gets verified
by reading the migration; this one has to earn its behaviour first, against tables the test
owns, so that when the outbox's `downgrade()` calls it the only new question is whether it was
called with the right arguments.

Nothing here touches outbox schema or outbox statuses -- the guard is deliberately
table-agnostic, and these tests use a throwaway table so they cannot quietly become a
specification of a design that has not been implemented yet.

Engine: SQLite. The guard issues one `SELECT COUNT(*) ... WHERE status IN (...)` with bound
parameters and one `has_table` inspection, none of which is engine-specific -- but it has
**not** been run against PostgreSQL, and the real migration's downgrade must be before the
outbox ships.
"""

import pytest
from sqlalchemy import create_engine, inspect, text

from app.database.downgrade_guard import OutstandingObligations, refuse_if_outstanding

TABLE = "cleanup_obligations"
UNRESOLVED = ("pending", "failed_retryable")
OBLIGATION = "an object that has not been erased yet"


def guard(connection, **kwargs):
    kwargs.setdefault("table", TABLE)
    kwargs.setdefault("unresolved", UNRESOLVED)
    kwargs.setdefault("obligation", OBLIGATION)
    return refuse_if_outstanding(connection, **kwargs)


@pytest.fixture()
def connection(tmp_path):
    """A real file-backed database, so a drop is a real drop."""
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    with engine.begin() as conn:
        yield conn
    engine.dispose()


def make_table(connection, rows=()):
    connection.execute(
        text(f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, status TEXT, storage_key TEXT)")
    )
    for index, (status, key) in enumerate(rows, start=1):
        connection.execute(
            text(f"INSERT INTO {TABLE} (id, status, storage_key) VALUES (:i, :s, :k)"),
            {"i": index, "s": status, "k": key},
        )


class TestItAllowsTheDowngradeWhenThereIsNothingToProtect:
    def test_an_absent_table_is_not_an_error(self, connection):
        # What a downgrade looks like on a database that never had the revision -- and what a
        # re-run of an already-completed downgrade looks like. Both must proceed.
        assert not inspect(connection).has_table(TABLE)
        guard(connection)

    def test_an_empty_table_is_not_an_error(self, connection):
        make_table(connection)
        guard(connection)

    def test_rows_in_terminal_statuses_are_not_an_error(self, connection):
        # The queue did its job; dropping it discards history, not obligations.
        make_table(connection, [("completed", "resumes/1.pdf"), ("failed_permanent", "resumes/2.pdf")])
        guard(connection)

    def test_it_can_be_called_twice(self, connection):
        make_table(connection, [("completed", "resumes/1.pdf")])
        guard(connection)
        guard(connection)


class TestItRefusesWhileWorkIsOutstanding:
    @pytest.mark.parametrize("status", UNRESOLVED)
    def test_a_single_unresolved_row_stops_the_downgrade(self, connection, status):
        make_table(connection, [(status, "resumes/7_abc.pdf")])
        with pytest.raises(OutstandingObligations) as excinfo:
            guard(connection)
        assert TABLE in str(excinfo.value)

    def test_it_counts_only_the_unresolved_rows(self, connection):
        make_table(
            connection,
            [
                ("pending", "a"),
                ("completed", "b"),
                ("failed_retryable", "c"),
                ("failed_permanent", "d"),
                ("pending", "e"),
            ],
        )
        with pytest.raises(OutstandingObligations) as excinfo:
            guard(connection)
        # Three unresolved of five rows. A count that included terminal rows would make the
        # guard fire forever once anything had ever been queued.
        assert "holds 3 unresolved row(s)" in str(excinfo.value)

    def test_the_message_says_what_to_do_and_that_there_is_no_override(self, connection):
        make_table(connection, [("pending", "a")])
        with pytest.raises(OutstandingObligations) as excinfo:
            guard(connection)
        message = str(excinfo.value)
        assert "Drain the queue first" in message
        assert "no override flag" in message
        assert OBLIGATION in message

    def test_a_status_value_containing_a_quote_is_bound_not_interpolated(self, connection):
        # Statuses are data and come through as bound parameters. If they were interpolated,
        # this would be a syntax error rather than a clean no-match.
        make_table(connection, [("completed", "a")])
        guard(connection, unresolved=["pend'ing", "fail\"ed"])


class TestItLeaksNothingAboutThePerson:
    def test_no_row_content_reaches_the_message(self, connection):
        # Migration output lands in deploy logs. The rows being protected describe someone who
        # asked to be forgotten, so the count is the only thing that may be reported.
        make_table(connection, [("pending", "resumes/4242_d3adb33f.pdf")])
        with pytest.raises(OutstandingObligations) as excinfo:
            guard(connection)
        message = str(excinfo.value)
        assert "d3adb33f" not in message
        assert "resumes/" not in message
        assert "4242" not in message


class TestNothingIsDroppedWhenTheGuardRefuses:
    """The ordering requirement, demonstrated on a real table rather than asserted about."""

    def test_the_table_survives_a_refused_downgrade(self, connection):
        make_table(connection, [("pending", "a")])

        def downgrade():
            # The shape the outbox migration's downgrade() will have: guard, then drop.
            guard(connection)
            connection.execute(text(f"DROP TABLE {TABLE}"))

        with pytest.raises(OutstandingObligations):
            downgrade()

        assert inspect(connection).has_table(TABLE)
        assert connection.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 1

    def test_a_drained_queue_lets_the_drop_through(self, connection):
        # The guard must not be so strict that a legitimate rollback is impossible after the
        # queue is drained, or operators will route around it.
        make_table(connection, [("completed", "a")])
        guard(connection)
        connection.execute(text(f"DROP TABLE {TABLE}"))
        assert not inspect(connection).has_table(TABLE)


class TestCallerMistakesFailLoudly:
    @pytest.mark.parametrize("bad", ["deletion jobs", "jobs; DROP TABLE users", 'jobs"', "", "1jobs"])
    def test_a_table_name_that_is_not_an_identifier_is_rejected(self, connection, bad):
        # Identifiers cannot be bound parameters, so they are interpolated -- and therefore
        # validated. A typo containing a quote fails as a bad identifier, not as SQL.
        with pytest.raises(ValueError, match="not a plain SQL identifier"):
            guard(connection, table=bad)

    def test_a_status_column_that_is_not_an_identifier_is_rejected(self, connection):
        with pytest.raises(ValueError, match="not a plain SQL identifier"):
            guard(connection, status_column="status FROM users --")

    def test_an_empty_unresolved_list_is_rejected(self, connection):
        # A guard that matches no status would pass silently forever, which is worse than no
        # guard: it reads as protection.
        make_table(connection, [("pending", "a")])
        with pytest.raises(ValueError, match="at least one status"):
            guard(connection, unresolved=[])
