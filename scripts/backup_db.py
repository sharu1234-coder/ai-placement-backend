#!/usr/bin/env python
"""Back up the database configured by DATABASE_URL.

PostgreSQL: runs pg_dump in custom format (-Fc) -- compressed, and the only format
pg_restore's --clean/--if-exists options (used by restore_db.py) support.
SQLite (the dev default): just copies the database file. pg_dump doesn't apply to SQLite,
and a file copy is a real, valid backup for a single-file database.

Usage:
    python scripts/backup_db.py [--out-dir backups]

Requires `pg_dump` on PATH for Postgres. If it's installed but not on PATH (common on
Windows), set PG_DUMP_PATH to its full path.

See OPERATIONS.md for the full backup/restore runbook, including why this isn't wired up to
run automatically anywhere yet (no production database exists to back up -- see that doc
before assuming backups are "active").
"""
import argparse
import datetime
import shutil
import subprocess
import sys
from pathlib import Path

# Imports app.core.config directly (not the full app) -- this must not trigger
# validate_for_production() (that only runs via app/main.py's module-level call), and it
# doesn't: app.core.config only *defines* that function, it doesn't call it on import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.config import settings  # noqa: E402
from app.database.pg_client import child_env, parse_target, redact_output  # noqa: E402


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_sqlite(db_url: str, out_dir: Path) -> Path:
    # sqlite:///./app.db -> ./app.db ; sqlite:////absolute/path.db -> /absolute/path.db
    raw_path = db_url.removeprefix("sqlite:///")
    db_path = Path(raw_path)
    if not db_path.exists():
        raise SystemExit(f"SQLite database file not found: {db_path}")

    out_path = out_dir / f"{db_path.stem}_{_timestamp()}.db"
    shutil.copy2(db_path, out_path)
    return out_path


def backup_postgres(db_url: str, out_dir: Path) -> Path:
    import os

    pg_dump = os.environ.get("PG_DUMP_PATH") or shutil.which("pg_dump")
    if not pg_dump:
        raise SystemExit(
            "pg_dump not found on PATH. Install the PostgreSQL client tools, or set "
            "PG_DUMP_PATH to its full path (e.g. on Windows: "
            r'"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe").'
        )

    # Parsed once, then passed explicitly. A SQLAlchemy URL is not a libpq connection
    # string: see app/database/pg_client.py for what happens when one is handed over whole.
    target = parse_target(db_url)
    out_path = out_dir / f"{target.database}_{_timestamp()}.dump"

    # -Fc: custom format -- compressed, and required by pg_restore's --clean/--if-exists
    # (used by restore_db.py) to selectively drop-and-recreate objects before restoring.
    result = subprocess.run(
        [pg_dump, "-Fc", "-f", str(out_path), *target.args()],
        capture_output=True,
        text=True,
        env=child_env(target),
    )
    if result.returncode != 0:
        out_path.unlink(missing_ok=True)
        detail = redact_output(result.stderr, target.password)
        raise SystemExit(f"pg_dump failed against {target.redacted()}:\n{detail}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir", default="backups", help="Directory to write the backup into (default: backups/)"
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    db_url = settings.database_url
    if db_url.startswith("sqlite"):
        out_path = backup_sqlite(db_url, out_dir)
    elif db_url.startswith("postgresql"):
        out_path = backup_postgres(db_url, out_dir)
    else:
        raise SystemExit(f"Unsupported DATABASE_URL scheme: {db_url.split(':', 1)[0]}")

    size_kb = out_path.stat().st_size / 1024
    print(f"Backup written: {out_path} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
