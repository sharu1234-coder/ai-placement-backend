"""Run every job alert that is currently due. Invoked by a scheduler, never by the web process.

This script is the missing *invocation surface*, not new alert logic. Everything it needs
already exists and is tested: `alerts.is_due` decides whether an alert should run now, and
`alerts.run_alert` runs one and records the outcome. What the codebase had no way to do was
iterate alerts across all profiles and call them, which is why alerts could be configured but
never actually fired.

**Why a separate process.** A scheduler living inside the API process would fire once per
worker (so N workers means N duplicate runs), would be silently lost on a restart or a scale
to zero, and would tie up a request worker for the length of a provider call. Running it as a
cron job or a worker dyno keeps the web tier stateless and makes "did alerts run" a question
with an answer in the scheduler's own logs.

**Why this is safe to run more than once.** `alerts.is_due` reads `last_run_at`, and
`run_alert` stamps it before doing any work, so a second invocation while the first is still
going finds nothing due. Delivery rows are additionally protected by a unique constraint whose
IntegrityError `alerts.py` treats as "already told them" -- see the savepoint handling there.
A cron that fires hourly is therefore fine, and a cron that double-fires is fine too.

**Failure policy.** `run_alert` never raises for a provider outage; it records the failure so
a student can be told the difference between "nothing new" and "we couldn't look". This script
keeps that property for everything else: one alert that fails for an unexpected reason is
logged and skipped, and the remaining alerts still run. A single bad row must not stop the
whole night's delivery.

Usage:

    python scripts/run_due_alerts.py              # run every due alert
    python scripts/run_due_alerts.py --dry-run    # report what would run, change nothing
    python scripts/run_due_alerts.py --limit 50   # cap the batch

Exit codes: 0 when the batch completed (including alerts that individually failed and were
recorded), 1 only when the batch itself could not run -- a database that cannot be reached, for
example. A scheduler should alert on 1.
"""

import argparse
import datetime
import logging
import sys
from pathlib import Path
from typing import List

from sqlalchemy.orm import Session

# Same as scripts/backup_db.py: run as `python scripts/run_due_alerts.py` from the repo root
# without needing PYTHONPATH set, which is what a cron line will do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.database.session import SessionLocal  # noqa: E402
from app.models.job_alert import JobAlert  # noqa: E402
from app.models.profile import StudentProfile  # noqa: E402
from app.services.job_hunt import alerts  # noqa: E402
from app.services.job_hunt.providers.factory import get_job_provider  # noqa: E402

logger = logging.getLogger("run_due_alerts")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _due_alerts(db: Session, now: datetime.datetime, limit: int) -> List[JobAlert]:
    """Enabled alerts that are due, oldest-run first.

    Ordered by `last_run_at` ascending with nulls first so a never-run alert and the
    longest-waiting alert go before one that ran recently. That ordering is what stops a
    `--limit` batch from starving the same alerts every night.

    The `is_due` filter runs in Python rather than SQL on purpose: it encodes quiet hours,
    weekday selection and the minimum gap between runs, and reimplementing that as a WHERE
    clause would be a second copy of the rule, free to disagree with the one the tests cover.
    The candidate set is bounded by "enabled", which is small.
    """
    candidates = (
        db.query(JobAlert)
        .filter(JobAlert.is_enabled.is_(True))
        .order_by(JobAlert.last_run_at.asc().nullsfirst(), JobAlert.id.asc())
        .all()
    )
    due = [alert for alert in candidates if alerts.is_due(alert, now)]
    return due[:limit] if limit else due


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Run every job alert that is currently due.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would run; change nothing.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum alerts to run (0 = no cap).")
    parser.add_argument("--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    now = _utcnow()
    db = None
    try:
        # Inside the try: connecting is itself a thing that fails in production, and a
        # scheduler needs the exit code rather than a traceback.
        db = SessionLocal()
        due = _due_alerts(db, now, args.limit)
        logger.info("alert batch starting: %d due at %s", len(due), now.isoformat())

        if args.dry_run:
            for alert in due:
                logger.info("would run alert id=%s profile=%s", alert.id, alert.profile_id)
            logger.info("dry run complete: %d alert(s) would have run", len(due))
            return 0

        ran = failed = 0
        provider = get_job_provider()
        for alert in due:
            profile = db.get(StudentProfile, alert.profile_id)
            if profile is None:
                # The profile was deleted between selecting and running. Not an error worth
                # failing a batch over, and not something to invent a run for.
                logger.warning("alert id=%s skipped: profile %s no longer exists", alert.id, alert.profile_id)
                continue
            try:
                status, new_jobs = alerts.run_alert(db, alert, profile, provider, now=now)
                db.commit()
                ran += 1
                logger.info("alert id=%s profile=%s status=%s new_jobs=%d",
                            alert.id, alert.profile_id, status, new_jobs)
            except Exception:
                # One bad alert must not take the batch down. Rolled back so the next alert
                # starts from a clean session -- on PostgreSQL a failed statement poisons the
                # transaction until it is, and every subsequent alert would fail too.
                db.rollback()
                failed += 1
                logger.exception("alert id=%s profile=%s raised unexpectedly", alert.id, alert.profile_id)

        logger.info("alert batch complete: %d ran, %d failed, %d due", ran, failed, len(due))
        return 0
    except Exception:
        logger.exception("alert batch could not run")
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
