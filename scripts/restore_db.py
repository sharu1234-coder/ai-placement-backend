#!/usr/bin/env python
"""Restore the database configured by DATABASE_URL from a backup file made by
scripts/backup_db.py.

DESTRUCTIVE: this replaces the target database's contents. Requires --yes to actually run
(otherwise it just prints what it would do). Refuses to run against ENVIRONMENT=production
unless --allow-production is also passed -- a safeguard against a local restore accidentally
pointed at a real production DATABASE_URL, not a statement that this is how a real production
restore should be performed (see OPERATIONS.md's incident-recovery section for that).

Usage:
    python scripts/restore_db.py backups/ai_placement_20260824T120000Z.dump --yes

Requires `pg_restore` on PATH for Postgres (see PG_RESTORE_PATH override in backup_db.py's
docstring -- same idea).
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.config import settings  # noqa: E402


def restore_sqlite(db_url: str, backup_path: Path) -> None:
    raw_path = db_url.removeprefix("sqlite:///")
    db_path = Path(raw_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, db_path)


def restore_postgres(db_url: str, backup_path: Path) -> None:
    import os

    pg_restore = os.environ.get("PG_RESTORE_PATH") or shutil.which("pg_restore")
    if not pg_restore:
        raise SystemExit(
            "pg_restore not found on PATH. Install the PostgreSQL client tools, or set "
            "PG_RESTORE_PATH to its full path."
        )

    # --clean --if-exists: drop existing objects before recreating them, so this works
    # whether the target database is empty or already has the old (soon to be replaced)
    # data -- --if-exists avoids erroring on objects that don't exist yet.
    # --no-owner: don't try to reassign ownership to roles that may not exist on the
    # restore target (e.g. a different managed-Postgres provider than where the dump was
    # taken).
    result = subprocess.run(
        [pg_restore, "--clean", "--if-exists", "--no-owner", "-d", db_url, str(backup_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"pg_restore failed:\n{result.stderr}")
    if result.stderr:
        # pg_restore routinely prints non-fatal warnings (e.g. "role does not exist" for
        # --no-owner) to stderr even on success (returncode 0) -- surfaced for visibility,
        # not treated as failure.
        print(f"pg_restore warnings:\n{result.stderr}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_file", help="Path to a backup file made by scripts/backup_db.py")
    parser.add_argument("--yes", action="store_true", help="Actually perform the restore")
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="Required in addition to --yes if ENVIRONMENT=production",
    )
    args = parser.parse_args()

    backup_path = Path(args.backup_file)
    if not backup_path.exists():
        raise SystemExit(f"Backup file not found: {backup_path}")

    db_url = settings.database_url

    if settings.environment == "production" and not args.allow_production:
        raise SystemExit(
            "ENVIRONMENT=production and --allow-production was not passed -- refusing to "
            "run. This is a safeguard against a local restore accidentally pointed at a "
            "real production DATABASE_URL, not a recommended way to actually restore "
            "production (see OPERATIONS.md)."
        )

    print(f"About to restore {backup_path} into: {db_url}")
    if not args.yes:
        print("Dry run -- pass --yes to actually perform the restore.")
        return

    if db_url.startswith("sqlite"):
        restore_sqlite(db_url, backup_path)
    elif db_url.startswith("postgresql"):
        restore_postgres(db_url, backup_path)
    else:
        raise SystemExit(f"Unsupported DATABASE_URL scheme: {db_url.split(':', 1)[0]}")

    print("Restore complete.")


if __name__ == "__main__":
    main()
