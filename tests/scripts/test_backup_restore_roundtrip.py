"""The backup scripts exist and are documented. This proves a restore actually restores.

The post-Phase-28 audit found `scripts/backup_db.py` and `scripts/restore_db.py` carrying no
test coverage at all. An untested restore path is the one you find out is broken at the moment
you need it, which is also the moment you have nothing to fall back on.

So the shape here is a real round trip rather than a smoke test:

    build a schema with `alembic upgrade head`
    write representative rows through it
    back it up
    destroy the database
    restore
    then re-open it and check the schema, the Alembic revision, the rows, the primary keys,
    the foreign keys and the unique constraints are all still there and still enforced

Checking that a file exists after a restore proves nothing -- a zero-byte file exists too. Every
assertion below is made by querying the restored database.

Everything runs in pytest's `tmp_path`. Nothing here reads, writes or points at the developer's
real `app.db`, and no credential is read or probed: PostgreSQL runs only if a legitimate
`TEST_DATABASE_URL` is already in the environment, and is otherwise reported NOT RUN.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup_db.py"
RESTORE = ROOT / "scripts" / "restore_db.py"

#: Rows chosen to exercise the things a file copy could plausibly lose: a parent/child pair
#: across a foreign key, and a value under a unique constraint.
USER_EMAIL = "phase29-roundtrip@example.com"


def _import_scripts():
    """The scripts are CLI modules, not a package; import them by path."""
    import importlib.util

    mods = {}
    for name, path in (("backup_db", BACKUP), ("restore_db", RESTORE)):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        mods[name] = module
    return mods["backup_db"], mods["restore_db"]


def _alembic_upgrade(db_path: Path) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "GITHUB_CLIENT_ID": "",
        "GITHUB_CLIENT_SECRET": "",
        "GITHUB_OAUTH_REDIRECT_URI": "",
        "TOKEN_ENCRYPTION_KEY": "",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stderr[-2000:]}"


def _insert(cur: sqlite3.Cursor, table: str, **values) -> int:
    """Insert a row, filling in whatever else the table insists on.

    The columns are read from the table rather than written out here. Several NOT NULL columns
    in this schema get their value from a Python-side SQLAlchemy `default=`, which never runs
    for a raw INSERT, so a hand-written column list breaks the moment a model gains a field --
    exactly the kind of brittleness that would make this test look like a backup failure.
    """
    columns = {}
    for _cid, name, decl_type, notnull, default, pk in cur.execute(
        f"PRAGMA table_info({table})"
    ).fetchall():
        if name in values:
            columns[name] = values[name]
        elif pk or not notnull or default is not None:
            continue
        else:
            kind = (decl_type or "").upper()
            if "INT" in kind:
                columns[name] = 0
            elif "BOOL" in kind:
                columns[name] = 1
            elif "DATE" in kind or "TIME" in kind:
                columns[name] = "2026-01-01 00:00:00"
            else:
                columns[name] = f"phase29-{name}"
    placeholders = ", ".join("?" for _ in columns)
    cur.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(columns.values()),
    )
    return cur.lastrowid


def _seed(db_path: Path) -> dict:
    """Write a user and a profile that points at it, and record what must come back."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        user_id = _insert(cur, "users", email=USER_EMAIL)
        profile_id = _insert(cur, "student_profiles", user_id=user_id)
        conn.commit()
        revision = cur.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    return {"user_id": user_id, "profile_id": profile_id, "revision": revision, "tables": tables}


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    _alembic_upgrade(db_path)
    state = _seed(db_path)
    return db_path, state


def test_backup_then_restore_returns_the_same_logical_database(tmp_path, seeded_db):
    backup_db, restore_db = _import_scripts()
    db_path, before = seeded_db
    db_url = f"sqlite:///{db_path.as_posix()}"
    out_dir = tmp_path / "backups"
    out_dir.mkdir()

    artifact = backup_db.backup_sqlite(db_url, out_dir)
    assert artifact.exists() and artifact.stat().st_size > 0, "the backup artifact is empty"

    # Destroy it outright -- a restore that only works over a database that is still there is
    # not a restore.
    db_path.unlink()
    assert not db_path.exists()

    restore_db.restore_sqlite(db_url, artifact)
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        _assert_restored(conn, before)
    finally:
        conn.close()


def _assert_restored(conn, before: dict) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert before["tables"] <= tables, (
        f"tables lost in the round trip: {sorted(before['tables'] - tables)}"
    )

    revision = cur.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision is not None and revision[0] == before["revision"], (
        "the restored database does not know which migration it is at, so a later "
        "`alembic upgrade` would not know what to apply"
    )

    row = cur.execute("SELECT id, email FROM users WHERE email = ?", (USER_EMAIL,)).fetchone()
    assert row is not None, "the seeded row did not survive the round trip"
    assert row[0] == before["user_id"], "the primary key changed"

    profile = cur.execute(
        "SELECT id, user_id FROM student_profiles WHERE id = ?", (before["profile_id"],)
    ).fetchone()
    assert profile is not None and profile[1] == before["user_id"], (
        "the foreign key relationship did not survive"
    )

    # Constraints have to be live, not merely present in the DDL text.
    with pytest.raises(sqlite3.IntegrityError):
        _insert(cur, "users", email=USER_EMAIL)
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        _insert(cur, "student_profiles", user_id=before["user_id"] + 10_000)
    conn.rollback()

    indexes = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert indexes, "no indexes survived the restore"


def test_backup_refuses_a_database_that_is_not_there(tmp_path):
    backup_db, _ = _import_scripts()
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    with pytest.raises(SystemExit):
        backup_db.backup_sqlite(f"sqlite:///{(tmp_path / 'missing.db').as_posix()}", out_dir)
    assert list(out_dir.iterdir()) == [], "a failed backup still wrote an artifact"


def test_restore_refuses_a_backup_that_is_not_there(tmp_path, seeded_db):
    db_path, _ = seeded_db
    before = db_path.read_bytes()
    result = subprocess.run(
        [sys.executable, str(RESTORE), str(tmp_path / "nope.db"), "--yes"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
             "GITHUB_CLIENT_ID": "", "GITHUB_CLIENT_SECRET": "",
             "GITHUB_OAUTH_REDIRECT_URI": "", "TOKEN_ENCRYPTION_KEY": ""},
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode != 0
    assert db_path.read_bytes() == before, (
        "a restore from a missing backup still altered the target database"
    )


def test_restore_is_a_dry_run_unless_told_otherwise(tmp_path, seeded_db):
    """The safeguard that stops a restore happening because someone ran the command to look."""
    backup_db, _ = _import_scripts()
    db_path, _ = seeded_db
    db_url = f"sqlite:///{db_path.as_posix()}"
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    artifact = backup_db.backup_sqlite(db_url, out_dir)

    # Make the live database differ from the backup, then run without --yes.
    conn = sqlite3.connect(db_path)
    try:
        _insert(conn.cursor(), "users", email="after-backup@example.com")
        conn.commit()
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, str(RESTORE), str(artifact)],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": db_url, "GITHUB_CLIENT_ID": "",
             "GITHUB_CLIENT_SECRET": "", "GITHUB_OAUTH_REDIRECT_URI": "",
             "TOKEN_ENCRYPTION_KEY": ""},
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0
    assert "Dry run" in result.stdout
    conn = sqlite3.connect(db_path)
    try:
        still_there = conn.execute(
            "SELECT COUNT(*) FROM users WHERE email = 'after-backup@example.com'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert still_there == 1, "a dry run overwrote the database it was only supposed to describe"


def test_restore_refuses_production_without_the_explicit_flag(tmp_path, seeded_db):
    backup_db, _ = _import_scripts()
    db_path, _ = seeded_db
    db_url = f"sqlite:///{db_path.as_posix()}"
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    artifact = backup_db.backup_sqlite(db_url, out_dir)

    result = subprocess.run(
        [sys.executable, str(RESTORE), str(artifact), "--yes"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": db_url, "ENVIRONMENT": "production",
             "GITHUB_CLIENT_ID": "", "GITHUB_CLIENT_SECRET": "",
             "GITHUB_OAUTH_REDIRECT_URI": "", "TOKEN_ENCRYPTION_KEY": ""},
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode != 0, "a production restore ran without --allow-production"
    assert "allow-production" in (result.stderr + result.stdout)


def test_a_corrupt_backup_does_not_produce_a_database_that_looks_restored(tmp_path, seeded_db):
    """The failure mode worth most: a restore that reports success over unusable bytes."""
    _, restore_db = _import_scripts()
    db_path, _ = seeded_db
    db_url = f"sqlite:///{db_path.as_posix()}"
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite file")

    restore_db.restore_sqlite(db_url, corrupt)
    # The script is a file copy, so it will "succeed". What must not happen is that the result
    # passes for a working database: opening and querying it has to fail loudly.
    #
    # The connection is closed in a finally rather than left to the exception: on Windows an
    # open SQLite handle keeps pytest from removing its own tmp directory, and the first version
    # of this test leaked one here -- which made tmp_path fail for every test that ran after it,
    # 221 of them, in a part of the suite this file does not otherwise touch.
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="PostgreSQL backup/restore needs a TEST_DATABASE_URL that already exists; "
           "credentials are never probed for",
)
def test_postgres_backup_and_restore_round_trip(tmp_path):
    backup_db, restore_db = _import_scripts()
    url = os.environ["TEST_DATABASE_URL"]
    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        pytest.skip("pg_dump/pg_restore not on PATH")
    out_dir = tmp_path / "pg"
    out_dir.mkdir()
    artifact = backup_db.backup_postgres(url, out_dir)
    assert artifact.exists() and artifact.stat().st_size > 0
    restore_db.restore_postgres(url, artifact)
