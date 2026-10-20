"""The deletion worker's runner: shutdown, polling, cleanup and error behaviour.

No database and no storage. Every collaborator the loop touches is injected, so these tests
drive the control flow directly rather than waiting on a clock -- which is the point of the
injection in `run_forever`. The erasure behaviour itself is tested in
tests/unit/test_deletion_outbox.py and, on real PostgreSQL, in
tests/unit/test_deletion_outbox_postgres.py.
"""

import logging
import signal

import pytest

from app.workers import deletion_worker
from scripts import run_deletion_worker as runner


class FakeSession:
    def __init__(self) -> None:
        self.closed = 0
        self.rolled_back = 0

    def close(self) -> None:
        self.closed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


class FakeStorage:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class Harness:
    """Records sessions handed out and sleeps requested."""

    def __init__(self, reports=None, errors=None):
        self.sessions: list[FakeSession] = []
        self.storage = FakeStorage()
        self.sleeps: list[float] = []
        self.reports = list(reports or [])
        self.errors = list(errors or [])
        self.passes = 0

    def session_factory(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session

    def storage_factory(self) -> FakeStorage:
        return self.storage

    def sleeper(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def run_pass(self, _session, _storage, limit=10):
        self.passes += 1
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        if self.reports:
            return self.reports.pop(0)
        return deletion_worker.RunReport()


def _report(**kwargs) -> deletion_worker.RunReport:
    return deletion_worker.RunReport(**kwargs)


class TestASinglePass:
    def test_once_runs_exactly_one_pass_and_never_sleeps(self):
        harness = Harness()
        status = runner.run_forever(
            session_factory=harness.session_factory,
            storage_factory=harness.storage_factory,
            once=True,
            sleeper=harness.sleeper,
            run_pass=harness.run_pass,
        )
        assert status == 0
        assert harness.passes == 1
        assert harness.sleeps == [], "--once must not wait before exiting"

    def test_the_batch_size_reaches_the_worker(self):
        seen = {}

        def record(_session, _storage, limit=10):
            seen["limit"] = limit
            return deletion_worker.RunReport()

        harness = Harness()
        runner.run_forever(
            session_factory=harness.session_factory,
            storage_factory=harness.storage_factory,
            once=True,
            batch=37,
            sleeper=harness.sleeper,
            run_pass=record,
        )
        assert seen["limit"] == 37


class TestBoundedPolling:
    def test_it_sleeps_the_configured_interval_between_passes(self):
        harness = Harness()
        runner.run_forever(
            session_factory=harness.session_factory,
            storage_factory=harness.storage_factory,
            interval=12.5,
            sleeper=harness.sleeper,
            run_pass=harness.run_pass,
            max_passes=3,
        )
        assert harness.passes == 3
        assert harness.sleeps == [12.5, 12.5]

    def test_the_interval_bounds_are_enforced(self):
        assert runner.validated_interval(runner.DEFAULT_INTERVAL_SECONDS)
        with pytest.raises(ValueError, match="poll too aggressively"):
            runner.validated_interval(0)
        with pytest.raises(ValueError, match="pickup target"):
            runner.validated_interval(runner.MAX_INTERVAL_SECONDS + 1)

    def test_the_batch_bounds_are_enforced(self):
        assert runner.validated_batch(runner.DEFAULT_BATCH) == runner.DEFAULT_BATCH
        with pytest.raises(ValueError):
            runner.validated_batch(0)
        with pytest.raises(ValueError):
            runner.validated_batch(runner.MAX_BATCH + 1)

    def test_a_bad_interval_exits_two_without_touching_the_database(self):
        """Validation happens before any import that needs a reachable database."""
        assert runner.main(["--interval", "0"]) == 2
        assert runner.main(["--batch", "0"]) == 2

    def test_the_default_interval_is_inside_the_pickup_target(self):
        assert (
            runner.DEFAULT_INTERVAL_SECONDS
            < deletion_worker.PICKUP_TARGET.total_seconds()
        )


class TestGracefulShutdown:
    def test_a_signal_stops_the_loop_after_the_current_pass(self):
        harness = Harness()
        shutdown = runner.Shutdown()

        def run_then_signal(session, storage, limit=10):
            report = harness.run_pass(session, storage, limit=limit)
            shutdown._handle(signal.SIGTERM, None)  # arrives mid-pass
            return report

        status = runner.run_forever(
            session_factory=harness.session_factory,
            storage_factory=harness.storage_factory,
            sleeper=harness.sleeper,
            run_pass=run_then_signal,
            shutdown=shutdown,
            max_passes=10,
        )
        assert status == 0
        assert harness.passes == 1, "the pass in flight finishes, and no further pass starts"
        assert harness.sleeps == [], "a signalled loop must not sleep before exiting"

    def test_the_session_of_the_final_pass_is_still_closed(self):
        harness = Harness()
        shutdown = runner.Shutdown()
        shutdown._handle(signal.SIGINT, None)

        runner.run_forever(
            session_factory=harness.session_factory,
            storage_factory=harness.storage_factory,
            sleeper=harness.sleeper,
            run_pass=harness.run_pass,
            shutdown=shutdown,
        )
        assert harness.passes == 1, "an already-signalled loop still completes one pass"
        assert all(session.closed == 1 for session in harness.sessions)

    def test_the_flag_records_which_signal_arrived(self, caplog):
        shutdown = runner.Shutdown()
        assert not shutdown
        with caplog.at_level(logging.INFO):
            shutdown._handle(signal.SIGTERM, None)
        assert shutdown
        assert shutdown.signal_name == "SIGTERM"
        assert "finishing the current pass" in caplog.text

    def test_installing_the_handlers_does_not_raise(self):
        original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            runner.Shutdown().install()
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)


class TestResourceCleanup:
    def test_every_pass_gets_a_fresh_session_and_closes_it(self):
        harness = Harness()
        runner.run_forever(
            session_factory=harness.session_factory,
            storage_factory=harness.storage_factory,
            sleeper=harness.sleeper,
            run_pass=harness.run_pass,
            max_passes=3,
        )
        assert len(harness.sessions) == 3
        assert [session.closed for session in harness.sessions] == [1, 1, 1]

    def test_the_storage_provider_is_built_once_and_released(self):
        harness = Harness()
        runner.run_forever(
            session_factory=harness.session_factory,
            storage_factory=harness.storage_factory,
            sleeper=harness.sleeper,
            run_pass=harness.run_pass,
            max_passes=3,
        )
        assert harness.storage.closed == 1, "built once per process, released once"

    def test_a_provider_without_close_is_fine(self):
        class Plain:
            pass

        status = runner.run_forever(
            session_factory=FakeSession,
            storage_factory=Plain,
            once=True,
            run_pass=lambda *_a, **_k: deletion_worker.RunReport(),
        )
        assert status == 0

    def test_a_close_that_raises_does_not_change_the_exit_code(self):
        class Angry:
            def close(self):
                raise RuntimeError("provider refused to close")

        status = runner.run_forever(
            session_factory=FakeSession,
            storage_factory=Angry,
            once=True,
            run_pass=lambda *_a, **_k: deletion_worker.RunReport(),
        )
        assert status == 0

    def test_a_session_close_that_raises_does_not_lose_the_pass(self, caplog):
        class Stubborn(FakeSession):
            def close(self):
                raise RuntimeError("session refused to close")

        with caplog.at_level(logging.WARNING):
            status = runner.run_forever(
                session_factory=Stubborn,
                storage_factory=FakeStorage,
                once=True,
                run_pass=lambda *_a, **_k: deletion_worker.RunReport(deleted=1),
            )
        assert status == 0
        assert "session close() raised" in caplog.text


class TestErrorBehaviour:
    def test_one_failed_pass_is_logged_rolled_back_and_retried(self, caplog):
        harness = Harness(errors=[RuntimeError("transient blip"), None])
        with caplog.at_level(logging.ERROR):
            status = runner.run_forever(
                session_factory=harness.session_factory,
                storage_factory=harness.storage_factory,
                sleeper=harness.sleeper,
                run_pass=harness.run_pass,
                max_passes=2,
            )
        assert status == 0, "a transient failure must not take the worker down"
        assert harness.passes == 2
        assert harness.sessions[0].rolled_back == 1, (
            "a poisoned transaction must be rolled back before the next pass"
        )
        assert harness.sessions[0].closed == 1
        assert "deletion pass failed (1 consecutive)" in caplog.text

    def test_repeated_failures_exit_non_zero_for_the_supervisor(self, caplog):
        errors = [RuntimeError("down")] * runner.MAX_CONSECUTIVE_FAILURES
        harness = Harness(errors=errors)
        with caplog.at_level(logging.ERROR):
            status = runner.run_forever(
                session_factory=harness.session_factory,
                storage_factory=harness.storage_factory,
                sleeper=harness.sleeper,
                run_pass=harness.run_pass,
                max_passes=50,
            )
        assert status == 1
        assert harness.passes == runner.MAX_CONSECUTIVE_FAILURES
        assert "a supervisor should" in caplog.text
        assert harness.storage.closed == 1, "cleanup still runs on the failure path"

    def test_the_failure_counter_resets_after_a_good_pass(self):
        errors = [RuntimeError("blip")] * (runner.MAX_CONSECUTIVE_FAILURES - 1)
        errors += [None] + [RuntimeError("blip")] * (runner.MAX_CONSECUTIVE_FAILURES - 1)
        harness = Harness(errors=errors)
        status = runner.run_forever(
            session_factory=harness.session_factory,
            storage_factory=harness.storage_factory,
            sleeper=harness.sleeper,
            run_pass=harness.run_pass,
            max_passes=len(errors),
        )
        assert status == 0, "consecutive means consecutive, not cumulative"

    def test_a_failure_in_the_session_factory_is_survivable(self, caplog):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database unreachable")
            return FakeSession()

        with caplog.at_level(logging.ERROR):
            status = runner.run_forever(
                session_factory=flaky,
                storage_factory=FakeStorage,
                sleeper=lambda _s: None,
                run_pass=lambda *_a, **_k: deletion_worker.RunReport(),
                max_passes=2,
            )
        assert status == 0
        assert "deletion pass failed" in caplog.text


class TestTheCountersAreNonSensitive:
    def test_a_pass_logs_the_six_counters(self, caplog):
        report = _report(
            claimed=4, deleted=2, already_absent=1, retried=1, needs_attention=1,
            requests_closed=1,
        )
        with caplog.at_level(logging.INFO):
            runner.log_pass(report)
        text = caplog.text
        for fragment in (
            "claimed=4",
            "deleted=2",
            "already_absent=1",
            "retried=1",
            "needs_attention=1",
            "requests_closed=1",
        ):
            assert fragment in text

    def test_an_empty_pass_logs_nothing(self, caplog):
        """A worker polling a drained queue every minute must not fill the log."""
        with caplog.at_level(logging.INFO):
            runner.log_pass(deletion_worker.RunReport())
        assert caplog.text == ""

    def test_the_report_carries_no_keys_or_identifiers(self):
        """Counters by construction: there is no field a key or an email could occupy."""
        fields = set(vars(deletion_worker.RunReport()))
        assert fields == {
            "claimed",
            "deleted",
            "already_absent",
            "retried",
            "needs_attention",
            "requests_closed",
        }
        assert all(isinstance(value, int) for value in vars(deletion_worker.RunReport()).values())

    def test_the_summary_line_holds_only_counters(self, caplog):
        harness = Harness(reports=[_report(claimed=1, deleted=1, requests_closed=1)])
        with caplog.at_level(logging.INFO):
            runner.run_forever(
                session_factory=harness.session_factory,
                storage_factory=harness.storage_factory,
                once=True,
                run_pass=harness.run_pass,
            )
        assert "deletion worker stopped after 1 pass(es)" in caplog.text
        assert "://" not in caplog.text
        assert "@" not in caplog.text


class TestItIsNotStartedByTheWebApplication:
    def test_main_does_not_reference_the_runner(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2] / "app/main.py"
        ).read_text(encoding="utf-8")
        assert "run_deletion_worker" not in source
        assert "deletion_worker" not in source, (
            "the web process must not drain the erasure queue: it would run once per worker "
            "and vanish on a scale to zero"
        )

    def test_the_runner_reuses_the_worker_rather_than_reimplementing_it(self):
        import inspect as py_inspect

        source = py_inspect.getsource(runner)
        assert "deletion_worker.run_once" in source
        # Checked as *execution*, not as prose: the module's docstring legitimately explains
        # the SKIP LOCKED claim, so scanning for that string would only catch the explanation.
        # What must not be here is any SQL of its own, or any claiming of its own.
        for forbidden in (
            "text(",
            ".execute(",
            "with_for_update",
            "claim_batch",
            "execute_job",
            ".query(",
        ):
            assert forbidden not in source, (
                "claiming and erasing belong to app/workers/deletion_worker.py; a second "
                f"implementation here would be free to disagree with the tested one ({forbidden})"
            )
