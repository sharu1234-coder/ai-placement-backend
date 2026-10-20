"""The Phase 4 schema change, run rather than read.

Every other test in this suite builds its schema with `create_all` from the models, which
would pass identically if this migration were empty. Production is built by Alembic alone, so
the only way to know it is safe is to put real rows in, run it, and look.

Three things are genuinely at risk, and each is checked below.

*The rebuild.* SQLite cannot ALTER a column in place, so batch mode drops job_listings,
recreates it and copies the rows across. job_listings is referenced by saved_jobs,
job_listing_matches and job_alert_deliveries -- a rebuild that broke one of those references
would leave a student's tracker pointing at nothing, and no amount of reading the migration
source would show it.

*The widening.* `url` goes from NOT NULL to nullable. That cannot invalidate a stored value,
but a rebuild that copied columns positionally rather than by name could still scramble them,
so the seeded values are compared field by field rather than merely counted.

*The default for existing rows.* `created_by_profile_id` must come out NULL on every
pre-existing row. NULL means "shared provider content", which is exactly what all of them are.
A non-NULL default would silently assign one student ownership of every advert in the table --
and, through the new read guard, hide them from everyone else.
"""

import datetime
import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import settings
from tests import db_safety

# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()

_TABLE = "job_listings"
_PREVIOUS_REVISION = "b4e17c9d05af"
_THIS_REVISION = "c3a7f1d85e29"

# Two adverts from different providers, which is what the (provider, provider_job_id) unique
# constraint exists to keep apart. Both carry a URL, because before this revision they had to.
_SEEDED = [
    (1, "adzuna", "adz-1", "https://jobs.example.com/1", "Data Engineer", "Contoso"),
    (2, "careerjet", "cj-9", "https://jobs.example.invalid/9", "Data Analyst", "Fabrikam"),
]


@pytest.fixture()
def migration_target(tmp_path, monkeypatch):
    if _TEST_DATABASE_URL:
        base = make_url(_TEST_DATABASE_URL)
        name = f"manual_listing_migration_test_{uuid.uuid4().hex[:8]}"
        admin = db_safety.make_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        url = db_safety.hidden(
            base.set(database=name).render_as_string(hide_password=False)
        )
        monkeypatch.setattr(settings, "database_url", url)
        try:
            yield url
        finally:
            # In a finally, so a test that errors cannot leave the database behind, and
            # without FORCE -- see db_safety.drop_temp_database for why FORCE needs
            # privileges over the superuser that this role deliberately does not have.
            db_safety.drop_temp_database(admin, name)
    else:
        url = f"sqlite:///{tmp_path / 'manual_listing.db'}"
        monkeypatch.setattr(settings, "database_url", url)
        yield url


def _alembic_config() -> Config:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


def _columns(url: str) -> dict:
    engine = db_safety.make_engine(url)
    try:
        return {c["name"]: c for c in inspect(engine).get_columns(_TABLE)}
    finally:
        engine.dispose()


def _seed(url: str) -> None:
    """Rows exactly as they exist before this revision: two provider adverts, one of them
    saved by a student and scored for them, so the foreign keys are real and not decorative."""
    engine = db_safety.make_engine(url)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, is_active, created_at,"
                    " token_version) VALUES (1, 'pre@example.com', 'x', :active, :now, 0)"
                ),
                {"now": now, "active": True},
            )
            connection.execute(text("INSERT INTO student_profiles (id, user_id) VALUES (1, 1)"))
            for row_id, provider, provider_job_id, url_value, title, company in _SEEDED:
                connection.execute(
                    text(
                        "INSERT INTO job_listings (id, provider, provider_job_id, url, title,"
                        " company, salary_is_predicted, fetched_at)"
                        " VALUES (:id, :provider, :pjid, :url, :title, :company, FALSE, :now)"
                    ),
                    {
                        "id": row_id,
                        "provider": provider,
                        "pjid": provider_job_id,
                        "url": url_value,
                        "title": title,
                        "company": company,
                        "now": now,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO saved_jobs (id, profile_id, listing_id, status, source,"
                    " created_at, updated_at)"
                    " VALUES (1, 1, 1, 'interview', 'recommended', :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO job_listing_matches (id, profile_id, listing_id, total_score,"
                    " score_version, component_scores, matched_skills, missing_skills,"
                    " unknown_factors, rationale, calculated_at)"
                    " VALUES (1, 1, 1, 0.8, 'v1', '{}', '[]', '[]', '[]', '', :now)"
                ),
                {"now": now},
            )
    finally:
        engine.dispose()


def _listings(url: str):
    engine = db_safety.make_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT id, provider, provider_job_id, url, title, company"
                    f" FROM {_TABLE} ORDER BY id"
                )
            ).fetchall()
    finally:
        engine.dispose()


@pytest.fixture()
def seeded_at_previous_revision(migration_target):
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _seed(migration_target)
    return migration_target


def test_url_becomes_nullable(seeded_at_previous_revision):
    before = _columns(seeded_at_previous_revision)["url"]
    assert before["nullable"] is False, "the fixture must start from the old schema"

    command.upgrade(_alembic_config(), _THIS_REVISION)

    assert _columns(seeded_at_previous_revision)["url"]["nullable"] is True


def test_every_existing_listing_survives_the_rebuild_unchanged(seeded_at_previous_revision):
    """Field by field, not merely a row count: a rebuild that copied columns positionally
    would keep the count and put the company in the title."""
    before = _listings(seeded_at_previous_revision)

    command.upgrade(_alembic_config(), _THIS_REVISION)

    after = _listings(seeded_at_previous_revision)
    assert after == before
    assert [row[3] for row in after] == [
        "https://jobs.example.com/1",
        "https://jobs.example.invalid/9",
    ], "a provider advert's apply link is the one field it cannot lose"


def test_existing_rows_are_owned_by_nobody(seeded_at_previous_revision):
    """NULL means shared provider content. Anything else would hand one student ownership of
    every advert already in the table, and the new read guard would then hide them from
    everyone else."""
    command.upgrade(_alembic_config(), _THIS_REVISION)

    engine = db_safety.make_engine(seeded_at_previous_revision)
    try:
        with engine.connect() as connection:
            owners = connection.execute(
                text(f"SELECT created_by_profile_id FROM {_TABLE} ORDER BY id")
            ).fetchall()
    finally:
        engine.dispose()

    assert [row[0] for row in owners] == [None, None]


def test_rows_that_reference_a_listing_still_reference_it(seeded_at_previous_revision):
    """The real hazard of a SQLite batch rebuild. A student's saved job and their computed
    match both point at job_listings.id; if the rebuild broke either, the tracker would render
    a row with nothing behind it."""
    command.upgrade(_alembic_config(), _THIS_REVISION)

    engine = db_safety.make_engine(seeded_at_previous_revision)
    try:
        with engine.connect() as connection:
            saved = connection.execute(
                text(
                    "SELECT s.id, s.status, l.title FROM saved_jobs s"
                    " JOIN job_listings l ON l.id = s.listing_id"
                )
            ).fetchall()
            matched = connection.execute(
                text(
                    "SELECT m.id, m.total_score, l.title FROM job_listing_matches m"
                    " JOIN job_listings l ON l.id = m.listing_id"
                )
            ).fetchall()
    finally:
        engine.dispose()

    assert saved == [(1, "interview", "Data Engineer")], (
        "the saved job kept both its stage and its listing"
    )
    assert matched == [(1, 0.8, "Data Engineer")]


def test_the_unique_constraint_on_provider_identity_survives(seeded_at_previous_revision):
    """uq_job_listing_provider_id deduplicates provider adverts and arbitrates the race
    between two concurrent searches. Phase 4 leans on it a second time, to deduplicate a
    student re-adding the same manual link, so losing it in the rebuild would leave two
    features silently broken."""
    command.upgrade(_alembic_config(), _THIS_REVISION)

    engine = db_safety.make_engine(seeded_at_previous_revision)
    try:
        unique = {u["name"] for u in inspect(engine).get_unique_constraints(_TABLE)}
    finally:
        engine.dispose()

    assert "uq_job_listing_provider_id" in unique


def test_a_manual_listing_with_no_url_can_be_stored_afterwards(seeded_at_previous_revision):
    """The whole point. Before this revision the INSERT below is rejected outright, which is
    exactly why the migration exists rather than being a convenience."""
    command.upgrade(_alembic_config(), _THIS_REVISION)

    engine = db_safety.make_engine(seeded_at_previous_revision)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job_listings (id, provider, provider_job_id, url, title,"
                    " company, created_by_profile_id, salary_is_predicted, fetched_at)"
                    " VALUES (3, 'manual', '1:abc', NULL, 'Campus placement -- SDE',"
                    " 'Contoso', 1, FALSE, :now)"
                ),
                {"now": now},
            )
            row = connection.execute(
                text("SELECT url, created_by_profile_id FROM job_listings WHERE id = 3")
            ).one()
    finally:
        engine.dispose()

    assert row[0] is None
    assert row[1] == 1


def test_the_migration_is_reversible_while_every_url_is_present(seeded_at_previous_revision):
    """Downgrade restores NOT NULL, so it can only succeed while no link-less manual listing
    exists -- which is the seeded state here. That asymmetry is deliberate and documented in
    the migration: refusing is better than deleting a student's tracked jobs to make a
    downgrade succeed."""
    command.upgrade(_alembic_config(), _THIS_REVISION)
    before = _listings(seeded_at_previous_revision)

    command.downgrade(_alembic_config(), _PREVIOUS_REVISION)

    columns = _columns(seeded_at_previous_revision)
    assert columns["url"]["nullable"] is False
    assert "created_by_profile_id" not in columns
    assert _listings(seeded_at_previous_revision) == before, "no advert is lost going back"
