"""The ungraded-evaluation migration, run rather than read.

Same reasoning as test_professional_presence_migration.py and
test_job_preparation_plan_migration.py: every other test builds its schema with
`Base.metadata.create_all`, which reads the models and would pass just as happily if this
migration were empty. Production is built by Alembic alone.

What this one must get right is narrower than creating a table and easier to get wrong
quietly. It relaxes NOT NULL on eight columns of a table that already holds graded rows, so the
round trip runs against a database containing a real grade, and that grade is checked
afterwards -- a batch ALTER on SQLite rebuilds the table, and a rebuild that dropped or
rewrote existing scores would still leave the columns looking correct.

The downgrade is the interesting half: ungraded rows cannot exist under the old schema, so it
deletes them. That must remove ONLY the ungraded ones.
"""

import datetime
import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import settings

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

_TABLE = "interview_evaluations"
_SCORE_COLUMNS = (
    "technical_correctness",
    "relevance",
    "completeness",
    "reasoning",
    "structure",
    "clarity",
    "role_jd_coverage",
    "overall_score",
)
_PREVIOUS_REVISION = "e8691c85f409"
_THIS_REVISION = "0d6496be2213"


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    """A throwaway database this test owns -- it runs downgrades, which would wreck the suite's
    shared one. Mirrors the fixture in the two sibling migration tests."""
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"eval_migration_test_{uuid.uuid4().hex[:8]}"
        admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        url = base.set(database=name).render_as_string(hide_password=False)
        monkeypatch.setattr(settings, "database_url", url)
        yield url
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
    else:
        url = f"sqlite:///{tmp_path / 'eval_migration.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
    """Programmatic, never Config("alembic.ini") -- see the sibling tests for why."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


def _columns(url: str) -> dict:
    engine = create_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns(_TABLE)}
    finally:
        engine.dispose()


def _current_revision(url: str):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            return row[0] if row else None
    finally:
        engine.dispose()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def _insert_graded_evaluation(url: str) -> int:
    """A graded evaluation written the way every pre-migration row was: every score set."""
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (email, password_hash, created_at, is_active, token_version) "
                    "VALUES ('eval-migration@example.com', 'x', :now, :active, 0)"
                ),
                {"now": _now(), "active": True},
            )
            user_id = connection.execute(text("SELECT MAX(id) FROM users")).scalar()
            connection.execute(
                text("INSERT INTO student_profiles (user_id) VALUES (:user_id)"), {"user_id": user_id}
            )
            profile_id = connection.execute(text("SELECT MAX(id) FROM student_profiles")).scalar()
            connection.execute(
                text(
                    "INSERT INTO interviews (profile_id, type, mode, status, created_at) "
                    "VALUES (:profile_id, 'technical', 'text', 'completed', :now)"
                ),
                {"profile_id": profile_id, "now": _now()},
            )
            interview_id = connection.execute(text("SELECT MAX(id) FROM interviews")).scalar()
            connection.execute(
                text(
                    "INSERT INTO interview_evaluations "
                    "(interview_id, technical_correctness, relevance, completeness, reasoning, "
                    " structure, clarity, role_jd_coverage, overall_score, feedback_text, created_at) "
                    "VALUES (:interview_id, 0.81, 0.72, 0.63, 0.54, 0.45, 0.86, 0.77, 0.68, "
                    "        'a real grade', :now)"
                ),
                {"interview_id": interview_id, "now": _now()},
            )
            return connection.execute(text("SELECT MAX(id) FROM interview_evaluations")).scalar()
    finally:
        engine.dispose()


def _insert_ungraded_evaluation(url: str) -> int:
    """What the post-migration code writes when the AI review fails: no scores at all."""
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            profile_id = connection.execute(text("SELECT MAX(id) FROM student_profiles")).scalar()
            connection.execute(
                text(
                    "INSERT INTO interviews (profile_id, type, mode, status, created_at) "
                    "VALUES (:profile_id, 'full_simulation', 'text', 'completed', :now)"
                ),
                {"profile_id": profile_id, "now": _now()},
            )
            interview_id = connection.execute(text("SELECT MAX(id) FROM interviews")).scalar()
            connection.execute(
                text(
                    "INSERT INTO interview_evaluations (interview_id, feedback_text, created_at) "
                    "VALUES (:interview_id, 'could not be reviewed', :now)"
                ),
                {"interview_id": interview_id, "now": _now()},
            )
            return connection.execute(text("SELECT MAX(id) FROM interview_evaluations")).scalar()
    finally:
        engine.dispose()


def test_the_migration_round_trips_with_existing_grades_intact(migration_target):
    config = _alembic_config()

    # Stop one revision short, write a graded row, then apply the migration on top -- which is
    # what applying this to a live database actually looks like.
    command.upgrade(config, _PREVIOUS_REVISION)
    graded_id = _insert_graded_evaluation(migration_target)
    assert graded_id is not None

    before = _columns(migration_target)
    assert all(before[name]["nullable"] is False for name in _SCORE_COLUMNS)

    command.upgrade(config, _THIS_REVISION)

    after = _columns(migration_target)
    for name in _SCORE_COLUMNS:
        assert after[name]["nullable"] is True, f"{name} must accept NULL after the upgrade"
    # The columns that carry no grade must NOT have been relaxed along with them.
    assert after["feedback_text"]["nullable"] is False
    assert after["interview_id"]["nullable"] is False
    assert _current_revision(migration_target) == _THIS_REVISION

    engine = create_engine(migration_target)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT technical_correctness, overall_score, feedback_text "
                    "FROM interview_evaluations WHERE id = :id"
                ),
                {"id": graded_id},
            ).fetchone()
        assert row is not None, "the migration lost an existing evaluation"
        assert row[0] == 0.81 and row[1] == 0.68, "existing scores were rewritten"
        assert row[2] == "a real grade"
    finally:
        engine.dispose()


def test_a_null_score_is_accepted_only_after_the_upgrade(migration_target):
    from sqlalchemy.exc import IntegrityError

    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _insert_graded_evaluation(migration_target)

    with pytest.raises(IntegrityError):
        _insert_ungraded_evaluation(migration_target)

    command.upgrade(config, _THIS_REVISION)

    ungraded_id = _insert_ungraded_evaluation(migration_target)
    engine = create_engine(migration_target)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT overall_score, feedback_text FROM interview_evaluations WHERE id = :id"),
                {"id": ungraded_id},
            ).fetchone()
        assert row[0] is None
        assert row[1] == "could not be reviewed"
    finally:
        engine.dispose()


def test_the_downgrade_removes_only_the_ungraded_rows(migration_target):
    """The downgrade's one destructive act, pinned down. An ungraded row cannot exist under the
    old schema, so it goes -- but a graded one holds a real measurement and must survive."""
    config = _alembic_config()
    command.upgrade(config, _THIS_REVISION)
    graded_id = _insert_graded_evaluation(migration_target)
    ungraded_id = _insert_ungraded_evaluation(migration_target)

    command.downgrade(config, _PREVIOUS_REVISION)

    columns = _columns(migration_target)
    for name in _SCORE_COLUMNS:
        assert columns[name]["nullable"] is False, f"{name} must be NOT NULL again"
    assert _current_revision(migration_target) == _PREVIOUS_REVISION

    engine = create_engine(migration_target)
    try:
        with engine.connect() as connection:
            surviving = [
                r[0] for r in connection.execute(text("SELECT id FROM interview_evaluations")).fetchall()
            ]
            kept = connection.execute(
                text("SELECT overall_score FROM interview_evaluations WHERE id = :id"), {"id": graded_id}
            ).fetchone()
    finally:
        engine.dispose()

    assert graded_id in surviving, "the downgrade destroyed a real grade"
    assert ungraded_id not in surviving, "an ungraded row cannot exist under the old schema"
    assert kept[0] == 0.68, "the surviving grade was altered"

    # A downgrade that left something behind fails only on the next upgrade, which in practice
    # means during a rollback.
    command.upgrade(config, _THIS_REVISION)
    assert _current_revision(migration_target) == _THIS_REVISION
    assert all(_columns(migration_target)[name]["nullable"] is True for name in _SCORE_COLUMNS)


def test_the_migration_produces_the_nullability_the_model_declares(migration_target):
    """Guards against the migration and the model drifting apart, since everywhere else in the
    suite the schema comes from the model and the two are never compared."""
    from app.models.interview_evaluation import InterviewEvaluation

    command.upgrade(_alembic_config(), _THIS_REVISION)

    migrated = _columns(migration_target)
    for column in InterviewEvaluation.__table__.columns:
        if column.primary_key:
            continue
        assert column.name in migrated, f"model declares {column.name}, the migration does not create it"
        assert migrated[column.name]["nullable"] == column.nullable, (
            f"{column.name}: model says nullable={column.nullable}, "
            f"migration produced nullable={migrated[column.name]['nullable']}"
        )
