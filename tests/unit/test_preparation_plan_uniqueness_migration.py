"""The Phase 25 migration, run against real duplicate rows rather than read.

Every other test in this suite builds its schema with create_all from the models, which would pass
identically if this migration were empty. Production is built by Alembic alone, and this revision
does two things no ordinary one does: it deletes student-visible data before adding a constraint,
and it cannot add the constraint at all while duplicates exist.

What is at risk, and checked below:

*The canonical choice.* Which plan survives has to be the one the app already returns, or applying
this migration would silently change what a student sees. Both read paths order by id descending,
so the highest id survives -- and that is asserted here against seeded rows rather than trusted.

*The deletes.* Unlike the Phase 15 career-path merge, nothing is merged: a plan holds no student
progress, only generated prose about one job description, and the surviving row has its own newer
version of every section. So the test's job is to prove the right rows went and the right one
stayed, including when several students and several job descriptions are involved at once.

*The rebuild.* SQLite cannot add a table constraint in place, so batch mode drops
job_preparation_plans, recreates it and copies the rows across. The columns, the indexes and the
foreign keys all have to survive that, and reading the migration would not show it if they did not.

*The constraint.* It has to actually refuse a second row afterwards, on both engines, or nothing
above matters.

The duplicates are seeded through raw SQL at the previous revision, because the model now carries
the constraint: creating this data through it is impossible, which is the point of the phase.
"""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import settings

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

_PREVIOUS_REVISION = "7f3c21d9ab48"
_THIS_REVISION = "b1c47e0a52d9"
_CONSTRAINT = "uq_job_preparation_plan_per_jd"

_NOW = "2026-10-01 12:00:00"
_EMPTY = "[]"
_SUMMARY = '{"company_name": null, "job_title": "Analyst"}'

# The query the invariant is stated as, used by the tests and quoted in the phase report.
_DUPLICATE_QUERY = (
    "SELECT profile_id, jd_id, COUNT(*) FROM job_preparation_plans"
    " GROUP BY profile_id, jd_id HAVING COUNT(*) > 1"
)


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    """A throwaway database this test owns.

    The URL is injected by monkeypatching settings.database_url rather than through the Config:
    alembic/env.py overwrites sqlalchemy.url with settings.database_url on every run, so a URL
    passed through the Config is silently ignored and the migrations run against the developer's
    real database. Mirrors the fixture in test_career_path_uniqueness_migration.py.
    """
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"prep_plan_uniqueness_{uuid.uuid4().hex[:8]}"
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
        url = f"sqlite:///{tmp_path / 'prep_plan_uniqueness.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
    """Programmatic, never Config("alembic.ini"): passing a filename makes alembic/env.py call
    fileConfig(), which reconfigures logging process-wide and silences unrelated tests. Same
    reasoning as the other migration tests."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


def _at_previous_revision(url: str):
    command.upgrade(_alembic_config(), _PREVIOUS_REVISION)
    return create_engine(url)


def _upgrade():
    command.upgrade(_alembic_config(), _THIS_REVISION)


def _rows(url: str, sql: str, **params):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql), params).all()
    finally:
        engine.dispose()


def _seed_profiles(connection, count: int) -> None:
    for index in range(1, count + 1):
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active, created_at, token_version)"
                " VALUES (:id, :email, 'x', :active, :now, 0)"
            ),
            {"id": index, "email": f"student{index}@example.com", "active": True, "now": _NOW},
        )
        connection.execute(
            text("INSERT INTO student_profiles (id, user_id) VALUES (:id, :id)"), {"id": index}
        )


def _seed_jd(connection, jd_id: int, profile_id: int = 1) -> None:
    connection.execute(
        text(
            "INSERT INTO job_descriptions (id, profile_id, raw_text, source, created_at)"
            " VALUES (:id, :pid, 'a posting', 'paste', :now)"
        ),
        {"id": jd_id, "pid": profile_id, "now": _NOW},
    )


def _seed_plan(connection, plan_id: int, jd_id: int, profile_id: int, company: str) -> None:
    """One stored plan, at the previous revision's column set.

    company_name is the marker: it is how a test says which of several identical-looking rows it
    is looking at afterwards.
    """
    connection.execute(
        text(
            "INSERT INTO job_preparation_plans"
            " (id, jd_id, profile_id, company_name, job_title, job_summary, required_skills,"
            "  preparation_gaps, preparation_plan, interview_prep, created_at)"
            " VALUES (:id, :jd, :pid, :company, 'Analyst', :summary, :empty, :empty, :empty,"
            "         :empty, :now)"
        ),
        {
            "id": plan_id, "jd": jd_id, "pid": profile_id, "company": company,
            "summary": _SUMMARY, "empty": _EMPTY, "now": _NOW,
        },
    )


def _plans(url: str):
    return _rows(
        url,
        "SELECT id, profile_id, jd_id, company_name FROM job_preparation_plans ORDER BY id",
    )


def _assert_constraint_refuses_duplicates(url: str, profile_id: int = 1, jd_id: int = 1) -> None:
    engine = create_engine(url)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _seed_plan(connection, 999, jd_id, profile_id, "Refused")
    finally:
        engine.dispose()


def _assert_columns_indexes_and_foreign_keys_survived(url: str) -> None:
    """The batch rebuild drops and recreates the table; this is what proves it came back whole."""
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("job_preparation_plans")}
        assert {
            "id", "jd_id", "profile_id", "company_name", "job_title", "job_summary",
            "required_skills", "preparation_gaps", "preparation_plan", "interview_prep",
            "gap_guidance", "resume_recommendations", "project_connections", "created_at",
        } <= columns, f"the rebuild lost columns: {columns}"
        indexed = {
            tuple(index["column_names"])
            for index in inspector.get_indexes("job_preparation_plans")
        }
        assert ("jd_id",) in indexed, f"ix_job_preparation_plans_jd_id did not survive: {indexed}"
        assert ("profile_id",) in indexed, f"the profile_id index did not survive: {indexed}"
        referenced = {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in inspector.get_foreign_keys("job_preparation_plans")
        }
        assert (("jd_id",), "job_descriptions") in referenced, referenced
        assert (("profile_id",), "student_profiles") in referenced, referenced
    finally:
        engine.dispose()


# ==========================================================================================


def test_the_constraint_exists_and_refuses_a_second_plan_for_the_same_jd(migration_target):
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 1)
        _seed_jd(connection, 1)
    engine.dispose()

    _upgrade()

    constraints = {
        c["name"] for c in inspect(create_engine(migration_target)).get_unique_constraints(
            "job_preparation_plans"
        )
    }
    assert _CONSTRAINT in constraints, constraints
    engine = create_engine(migration_target)
    with engine.begin() as connection:
        _seed_plan(connection, 1, 1, 1, "Acme")
    engine.dispose()
    _assert_constraint_refuses_duplicates(migration_target)


def test_a_different_jd_or_a_different_student_is_never_refused(migration_target):
    """The constraint has to encode the business identity, not merely reduce rows.

    A unique index on jd_id alone would pass the test above and break the product: a job
    description belongs to one student, but nothing about the plan table says so, and getting this
    wrong is how two students end up sharing one plan.
    """
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 2)
        _seed_jd(connection, 1, profile_id=1)
        _seed_jd(connection, 2, profile_id=1)
        _seed_jd(connection, 3, profile_id=2)
    engine.dispose()

    _upgrade()

    engine = create_engine(migration_target)
    with engine.begin() as connection:
        _seed_plan(connection, 1, 1, 1, "One")
        _seed_plan(connection, 2, 2, 1, "Two")
        _seed_plan(connection, 3, 3, 2, "Three")
    engine.dispose()
    assert len(_plans(migration_target)) == 3


def test_shadowed_duplicates_are_removed_and_the_newest_plan_survives(migration_target):
    """The row that survives is the one every read path already returned.

    This is the property that makes the migration safe to apply to live data: a student's app
    shows the same plan before and after, because "newest id" is exactly what
    get_preparation_plan and the saved-jobs list both select.
    """
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 1)
        _seed_jd(connection, 1)
        _seed_plan(connection, 1, 1, 1, "First attempt")
        _seed_plan(connection, 2, 1, 1, "Second attempt")
        _seed_plan(connection, 3, 1, 1, "Third attempt")
    engine.dispose()
    assert len(_rows(migration_target, _DUPLICATE_QUERY)) == 1, "the duplicates were not seeded"

    _upgrade()

    remaining = _plans(migration_target)
    assert [row.id for row in remaining] == [3]
    assert remaining[0].company_name == "Third attempt"
    assert _rows(migration_target, _DUPLICATE_QUERY) == []
    _assert_constraint_refuses_duplicates(migration_target)
    _assert_columns_indexes_and_foreign_keys_survived(migration_target)


def test_each_student_and_job_description_is_resolved_independently(migration_target):
    """Several overlapping duplicate groups at once, which is what a real deployment has.

    A migration that grouped by jd_id alone, or that kept one row per student rather than one per
    pair, would pass the single-group test above and destroy data here.
    """
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 2)
        _seed_jd(connection, 1, profile_id=1)
        _seed_jd(connection, 2, profile_id=1)
        _seed_jd(connection, 3, profile_id=2)
        # profile 1, jd 1: three copies
        _seed_plan(connection, 1, 1, 1, "p1 jd1 old")
        _seed_plan(connection, 2, 1, 1, "p1 jd1 older")
        _seed_plan(connection, 7, 1, 1, "p1 jd1 newest")
        # profile 1, jd 2: a single plan, which must be left exactly as it is
        _seed_plan(connection, 3, 2, 1, "p1 jd2 only")
        # profile 2, jd 3: two copies
        _seed_plan(connection, 4, 3, 2, "p2 jd3 old")
        _seed_plan(connection, 8, 3, 2, "p2 jd3 newest")
    engine.dispose()

    _upgrade()

    assert [(row.id, row.company_name) for row in _plans(migration_target)] == [
        (3, "p1 jd2 only"),
        (7, "p1 jd1 newest"),
        (8, "p2 jd3 newest"),
    ]
    assert _rows(migration_target, _DUPLICATE_QUERY) == []


def test_a_database_with_no_duplicates_is_left_alone(migration_target):
    """The ordinary deployment. Nothing to resolve, and the migration must still add the
    constraint rather than skip it because it found nothing to do."""
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 1)
        _seed_jd(connection, 1)
        _seed_jd(connection, 2)
        _seed_plan(connection, 1, 1, 1, "Acme")
        _seed_plan(connection, 2, 2, 1, "Globex")
    engine.dispose()

    _upgrade()

    assert [(row.id, row.company_name) for row in _plans(migration_target)] == [
        (1, "Acme"),
        (2, "Globex"),
    ]
    _assert_constraint_refuses_duplicates(migration_target)


def test_the_migration_is_reversible_and_the_removals_stand(migration_target):
    """Downgrade drops the constraint. It does not bring deleted plans back, and says so.

    Asserted rather than left implicit: a reader of the migration should not be able to believe
    that downgrading restores the rows, and a future change that tried to "helpfully" recreate
    them would be inventing AI content that was never generated.
    """
    engine = _at_previous_revision(migration_target)
    with engine.begin() as connection:
        _seed_profiles(connection, 1)
        _seed_jd(connection, 1)
        _seed_plan(connection, 1, 1, 1, "Removed")
        _seed_plan(connection, 2, 1, 1, "Kept")
    engine.dispose()
    _upgrade()

    command.downgrade(_alembic_config(), _PREVIOUS_REVISION)

    constraints = {
        c["name"] for c in inspect(create_engine(migration_target)).get_unique_constraints(
            "job_preparation_plans"
        )
    }
    assert _CONSTRAINT not in constraints, constraints
    assert [(row.id, row.company_name) for row in _plans(migration_target)] == [(2, "Kept")]
    # And a duplicate is accepted again, which is what "the constraint is gone" means.
    engine = create_engine(migration_target)
    with engine.begin() as connection:
        _seed_plan(connection, 3, 1, 1, "Allowed once more")
    engine.dispose()
    assert len(_plans(migration_target)) == 2
