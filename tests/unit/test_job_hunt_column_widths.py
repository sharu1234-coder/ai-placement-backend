"""Provider text against the widths the database actually declares.

SQLite ignores `String(n)`. PostgreSQL raises StringDataRightTruncation. That difference is
invisible to every other test in this repository, and the values in question are chosen by a
third party: a provider returning one over-long field would pass the whole suite and then 500
the search page for every student in production.

It is not hypothetical. Careerjet's adapter copies `salary_currency_code` through unchecked
into a `String(3)` column, so a malformed response carrying "RUPEES-INDIAN" is enough.

The limits are read off the model rather than written out again, so widening a column does not
leave a test asserting the old number.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- imports every model so create_all sees the full schema
from app.database.base import Base
from app.models.job_listing import JobListing
from app.services.job_hunt import service
from app.services.job_hunt.providers.base import ProviderJob

NOW = datetime.datetime(2026, 9, 9, 12, 0, 0)


def column_limit(name: str) -> int:
    """The declared width of one job_listings column, straight from the model."""
    return JobListing.__table__.columns[name].type.length


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'widths.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class FakeProvider:
    name = "fake"
    attribution_text = "Jobs by Fake"
    attribution_url = None
    cache_ttl = datetime.timedelta(hours=6)

    def search(self, query):  # pragma: no cover -- the upsert is called directly
        raise NotImplementedError


def job(**overrides) -> ProviderJob:
    base = dict(
        provider="fake",
        provider_job_id="1",
        url="https://jobs.example.com/1",
        title="Data Analyst",
        company="Example Ltd",
        location="Bengaluru",
    )
    base.update(overrides)
    return ProviderJob(**base)


def store(db, provider_job) -> JobListing:
    listing = service._upsert_listing(db, provider_job, FakeProvider(), NOW)
    if listing is not None:
        db.commit()
    return listing


class TestEveryStoredValueFitsItsColumn:
    @pytest.mark.parametrize("field", ["title", "company", "location"])
    def test_over_long_display_text_is_truncated_rather_than_losing_the_job(self, db, field):
        """Display text is clipped, not dropped. No real advert comes near these limits, and
        losing a genuine vacancy over a long title would be the worse trade."""
        limit = column_limit(field)
        listing = store(db, job(**{field: "x" * (limit + 50)}))

        assert listing is not None
        assert len(getattr(listing, field)) == limit

    def test_a_currency_code_that_does_not_fit_becomes_unknown_not_a_prefix(self, db):
        """The real Careerjet shape. "RUP" is not a shorter way of writing "RUPEES-INDIAN" --
        it is a different currency, and this module never guesses at a value it does not have.
        The salary figures survive; only the unit is dropped, and the client renders a figure
        with no currency rather than one with the wrong currency."""
        listing = store(
            db, job(salary_currency="RUPEES-INDIAN", salary_min=900_000, salary_max=1_600_000)
        )

        assert listing.salary_currency is None
        assert listing.salary_min == 900_000

    def test_a_currency_code_that_does_fit_is_kept_exactly(self, db):
        assert store(db, job(salary_currency="INR")).salary_currency == "INR"

    def test_an_over_long_salary_period_becomes_unknown_rather_than_a_wrong_period(self, db):
        # "fortnightly"[:10] would be "fortnightl", which no reader and no code can interpret.
        listing = store(db, job(salary_period="fortnightly-ish", salary_min=500))
        assert listing.salary_period is None

    def test_an_over_long_url_drops_the_listing_because_a_clipped_link_goes_elsewhere(self, db):
        limit = column_limit("url")
        listing = store(db, job(url="https://jobs.example.com/" + "a" * limit))

        assert listing is None, "a truncated URL is a link to a different page"
        assert db.query(JobListing).count() == 0

    def test_an_over_long_provider_id_drops_the_listing_because_a_clipped_id_collides(self, db):
        """The id is half of the uniqueness constraint. Two adverts whose ids differ only past
        the limit would clip to the same value and the second would overwrite the first."""
        limit = column_limit("provider_job_id")
        listing = store(db, job(provider_job_id="z" * (limit + 1)))

        assert listing is None
        assert db.query(JobListing).count() == 0

    def test_a_refreshed_listing_is_clamped_too_not_just_a_newly_inserted_one(self, db):
        """The update branch writes the same columns. Clamping only on insert would mean the
        second time an advert is seen is the time it breaks."""
        store(db, job(title="Data Analyst"))
        limit = column_limit("title")

        refreshed = store(db, job(title="y" * (limit + 50)))

        assert len(refreshed.title) == limit
        assert db.query(JobListing).count() == 1

    def test_an_ordinary_advert_passes_through_completely_unchanged(self, db):
        """The guard against over-clamping: nothing about a normal listing may be altered."""
        original = job(
            title="Senior Data Analyst",
            company="Example Ltd",
            location="Bengaluru, Karnataka",
            salary_currency="INR",
            salary_period="year",
            salary_min=900_000,
        )
        listing = store(db, original)

        assert listing.title == original.title
        assert listing.company == original.company
        assert listing.location == original.location
        assert listing.salary_currency == "INR"
        assert listing.salary_period == "year"
        assert listing.salary_min == 900_000


class TestNoStoredStringCanExceedItsColumn:
    def test_a_listing_built_entirely_from_over_long_provider_text_still_fits(self, db):
        """The sweep. Every String column on job_listings is checked against its own declared
        width, so a column added later is covered without this test being edited."""
        listing = store(
            db,
            job(
                title="t" * 4000,
                company="c" * 4000,
                location="l" * 4000,
                work_mode="w" * 4000,
                employment_type="e" * 4000,
                seniority="s" * 4000,
                salary_currency="INR",
                salary_period="year",
            ),
        )
        assert listing is not None

        for column in JobListing.__table__.columns:
            limit = getattr(column.type, "length", None)
            if limit is None:
                continue  # Text and non-string columns carry no width
            value = getattr(listing, column.name)
            if isinstance(value, str):
                assert len(value) <= limit, f"{column.name} is {len(value)} > {limit}"
