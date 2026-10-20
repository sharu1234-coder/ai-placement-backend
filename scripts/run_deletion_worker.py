"""Run the account-deletion outbox worker. Invoked by a scheduler or a supervisor, never by
the web process.

This is the missing *invocation surface*, not new deletion logic. Everything it needs exists
and is tested: `deletion_worker.run_once` claims a bounded batch with `FOR UPDATE SKIP
LOCKED`, erases each object, records each outcome in its own transaction, and closes a request
only after re-counting inside the closing transaction. What the codebase had no way to do was
call that on a timer, which is why erasure obligations could be recorded but never discharged.

**Why a separate process.** A loop inside the API process would run once per web worker, be
silently lost on a restart or a scale to zero, and tie a request worker to storage latency.
`app/main.py` deliberately does not start this -- a test asserts that -- so "did the queue
drain" is a question with an answer in this process's own logs.

**Why running two of these is safe.** The claim is `SELECT ... FOR UPDATE SKIP LOCKED` plus a
lease column, so a second instance steps over rows the first holds rather than waiting for
them or taking them twice; that behaviour is proven against PostgreSQL in
tests/unit/test_deletion_outbox_postgres.py. No leader election, no advisory lock, no
singleton deployment requirement. A hard kill is safe too: the lease expires and the job
returns to the queue.

**What it does not do.** It never widens a privilege, never uses `DROP DATABASE ... WITH
(FORCE)`, and logs no storage key, credential, connection URL or user detail -- only counters,
job ids and request uuids. On SQLite the claim has no `SKIP LOCKED` (the dialect has neither
the clause nor row locks), so a single instance is the only safe configuration there; that is
a development convenience, not the deployment target.

Usage:

    python scripts/run_deletion_worker.py --once           # one pass, then exit
    python scripts/run_deletion_worker.py                  # poll until signalled
    python scripts/run_deletion_worker.py --interval 30 --batch 25

Exit codes: 0 when the loop ended cleanly (including passes in which individual jobs failed
and were requeued), 1 when the loop itself could not run -- an unreachable database, say, or
too many consecutive failures. A supervisor should restart on 1 and alert on a crash loop.
"""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Optional

# Same as scripts/run_due_alerts.py: runnable as `python scripts/run_deletion_worker.py` from
# the repository root with no PYTHONPATH, which is what a cron line or a unit file will do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.workers import deletion_worker  # noqa: E402

logger = logging.getLogger("run_deletion_worker")

# Defaults, bounded. The interval is well inside the provisional 5-minute pickup target from
# docs/proposal-account-deletion-outbox.md section 11.2, so a queue that is being served never
# approaches it -- and a queue that does approach it means this process is not running, which
# is a monitoring signal rather than something to poll harder for.
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_BATCH = 10
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 300
MIN_BATCH = 1
MAX_BATCH = 500

# A pass that raises is logged and retried, because a transient database blip should not take
# the worker down. Repeated failures exit non-zero so a supervisor restarts the process rather
# than leaving it spinning in the dark.
MAX_CONSECUTIVE_FAILURES = 5


class Shutdown:
    """A signal flag, checked between jobs rather than inside one.

    SIGINT and SIGTERM set it; the current pass finishes and commits, then the loop exits. The
    alternative -- raising out of the handler -- could interrupt a pass between the storage
    delete and the transaction that records it, which is survivable (the lease expires and the
    retry deletes an absent object) but needlessly reopens a window this process can simply
    close by waiting a moment.
    """

    def __init__(self) -> None:
        self.requested = False
        self.signal_name: Optional[str] = None

    def install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handle)

    def _handle(self, signum, _frame) -> None:
        self.requested = True
        self.signal_name = signal.Signals(signum).name
        logger.info("%s received; finishing the current pass and stopping", self.signal_name)

    def __bool__(self) -> bool:
        return self.requested


def validated_interval(value: float) -> float:
    """Reject an interval that would either hammer the database or miss the pickup target."""
    if not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"--interval must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS} "
            f"seconds; {value} would "
            + ("poll too aggressively" if value < MIN_INTERVAL_SECONDS else "risk the pickup target")
        )
    return value


def validated_batch(value: int) -> int:
    """Reject a batch that would make one pass unbounded in time or hold too many leases."""
    if not MIN_BATCH <= value <= MAX_BATCH:
        raise ValueError(f"--batch must be between {MIN_BATCH} and {MAX_BATCH}; got {value}")
    return value


def log_pass(report: deletion_worker.RunReport) -> None:
    """Counters only. No key, no uuid of a person, nothing a log aggregator should not hold."""
    if not report.claimed:
        return
    logger.info(
        "deletion pass: claimed=%d deleted=%d already_absent=%d retried=%d "
        "needs_attention=%d requests_closed=%d",
        report.claimed,
        report.deleted,
        report.already_absent,
        report.retried,
        report.needs_attention,
        report.requests_closed,
    )


def _close_storage(storage: object) -> None:
    """Release a provider that holds a client, if it has a way to be released.

    Duck-typed on purpose: `StorageProvider` defines no close, the local provider needs none,
    and an S3 client is released when it is dropped. This exists so a provider that *does*
    acquire something has a documented place to give it back.
    """
    close = getattr(storage, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - shutdown must not fail on cleanup
            logger.warning("storage provider close() raised during shutdown", exc_info=False)


def run_forever(
    *,
    session_factory: Callable[[], object],
    storage_factory: Callable[[], object],
    interval: float = DEFAULT_INTERVAL_SECONDS,
    batch: int = DEFAULT_BATCH,
    once: bool = False,
    shutdown: Optional[Shutdown] = None,
    sleeper: Callable[[float], None] = time.sleep,
    run_pass: Callable[..., deletion_worker.RunReport] = deletion_worker.run_once,
    max_passes: Optional[int] = None,
) -> int:
    """Poll until signalled. Returns a process exit code.

    Every collaborator is injected so the loop can be tested without a database, a clock or a
    real signal: the tests drive `sleeper`, `run_pass` and `shutdown` directly.

    A fresh session per pass, closed in a `finally`. That is not ceremony -- on PostgreSQL a
    failed statement poisons the transaction until it is rolled back, and a session reused
    across passes would carry that into the next one.
    """
    shutdown = shutdown if shutdown is not None else Shutdown()
    storage = storage_factory()
    totals = deletion_worker.RunReport()
    failures = 0
    passes = 0
    status = 0

    try:
        while True:
            session = None
            try:
                session = session_factory()
                report = run_pass(session, storage, limit=batch)
                log_pass(report)
                totals.claimed += report.claimed
                totals.deleted += report.deleted
                totals.already_absent += report.already_absent
                totals.retried += report.retried
                totals.needs_attention += report.needs_attention
                totals.requests_closed += report.requests_closed
                failures = 0
            except Exception:
                failures += 1
                # No message interpolation of anything job-specific: the traceback is the
                # diagnostic, and it is written to this process's log, not to a user.
                logger.exception("deletion pass failed (%d consecutive)", failures)
                if session is not None:
                    try:
                        session.rollback()
                    except Exception:  # noqa: BLE001 - the session is going away anyway
                        pass
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "stopping after %d consecutive failed passes; a supervisor should "
                        "restart this process",
                        failures,
                    )
                    status = 1
                    break
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:  # noqa: BLE001 - cleanup must not mask the outcome
                        logger.warning("session close() raised", exc_info=False)

            passes += 1
            if once or shutdown or (max_passes is not None and passes >= max_passes):
                break
            sleeper(interval)
            if shutdown:
                break
    finally:
        _close_storage(storage)
        logger.info(
            "deletion worker stopped after %d pass(es): claimed=%d deleted=%d "
            "already_absent=%d retried=%d needs_attention=%d requests_closed=%d",
            passes,
            totals.claimed,
            totals.deleted,
            totals.already_absent,
            totals.retried,
            totals.needs_attention,
            totals.requests_closed,
        )

    return status


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the account-deletion outbox worker.")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"seconds between passes (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=DEFAULT_BATCH,
        help=f"maximum jobs claimed per pass (default: {DEFAULT_BATCH})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        interval = validated_interval(args.interval)
        batch = validated_batch(args.batch)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    # Imported here rather than at module scope so that --help and the argument validation do
    # not require a reachable database.
    from app.database.session import SessionLocal, engine
    from app.storage.provider import get_storage_provider

    shutdown = Shutdown()
    shutdown.install()

    try:
        return run_forever(
            session_factory=SessionLocal,
            storage_factory=get_storage_provider,
            interval=interval,
            batch=batch,
            once=args.once,
            shutdown=shutdown,
        )
    finally:
        # The engine is process-wide, so it is disposed once, here, rather than per pass.
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
