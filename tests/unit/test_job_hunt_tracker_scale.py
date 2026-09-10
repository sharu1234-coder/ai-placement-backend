"""The tracker at sizes the API tests never reach.

Two caps used to sit between a student and their own saved jobs, and neither announced itself:

  * `list_saved_jobs` defaulted to 100 rows and the tracker endpoint passed no limit, so a
    student with more than a hundred tracked applications simply stopped seeing the oldest of
    them. Nothing on screen said a row had been dropped.

  * search, home and saved-search-run each worked out "have I already saved this job?" by
    loading the student's 500 most recently updated saved rows and looking in that. Past 500,
    an older save was reported as unsaved -- so the card offered "Save" for a job already
    sitting in the tracker at "interview", and tapping it moved that row back to "saved".

Both need hundreds of rows to show up, which is why they are tested here against a real
session with bulk inserts rather than through the API, where the same coverage would mean
hundreds of HTTP round trips and a rate limit.
"""

import datetime
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- imports every model so create_all sees the full schema
from app.database.base import Base
from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.saved_job import SavedJob
from app.models.user import User
from app.services.job_hunt import service

NOW = datetime.datetime(2026, 9, 9, 12, 0, 0)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tracker.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def seed(db, count: int) -> tuple[int, list[int]]:
    """`count` listings, all saved by one student, each updated a minute after the last.

    The staggered `updated_at` is the point: both bugs were "take the most recent N", so the
    oldest row has to be genuinely oldest for its disappearance to mean anything.
    """
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()

    # Unique per call so a test that seeds two students does not collide on the shared
    # listings table -- listings are deliberately not profile-owned.
    batch = uuid.uuid4().hex[:8]
    listings = [
        JobListing(
            provider="fake",
            provider_job_id=f"{batch}-{index}",
            url=f"https://jobs.example.com/{batch}-{index}",
            title="Data Analyst",
            fetched_at=NOW,
        )
        for index in range(count)
    ]
    db.add_all(listings)
    db.commit()

    db.add_all(
        [
            SavedJob(
                profile_id=profile.id,
                listing_id=listing.id,
                status="saved",
                created_at=NOW,
                updated_at=NOW + datetime.timedelta(minutes=index),
            )
            for index, listing in enumerate(listings)
        ]
    )
    db.commit()
    return profile.id, [listing.id for listing in listings]


class TestTrackerListing:
    def test_more_than_a_hundred_tracked_jobs_are_all_returned(self, db):
        """120 saved, 120 listed. Before the fix this returned 100 and said nothing about the
        other 20 -- in the one screen where a missing row means a forgotten application."""
        profile_id, _listing_ids = seed(db, 120)

        rows = service.list_saved_jobs(db, profile_id)

        assert len(rows) == 120

    def test_the_documented_ceiling_is_enforced_and_is_not_the_old_silent_one(self, db):
        profile_id, _listing_ids = seed(db, service.MAX_TRACKER_ROWS + 25)

        rows = service.list_saved_jobs(db, profile_id)

        assert len(rows) == service.MAX_TRACKER_ROWS
        assert service.MAX_TRACKER_ROWS > 100

    def test_a_caller_cannot_ask_for_more_than_the_ceiling(self, db):
        """The limit is clamped here rather than trusted, so no caller can turn this into an
        unbounded read."""
        profile_id, _listing_ids = seed(db, service.MAX_TRACKER_ROWS + 25)

        rows = service.list_saved_jobs(db, profile_id, limit=100_000)

        assert len(rows) == service.MAX_TRACKER_ROWS


class TestSavedLookupForAPage:
    def test_the_oldest_save_is_still_found_past_the_old_five_hundred_row_window(self, db):
        """The failure this prevents is not cosmetic.

        A job reported as unsaved gets a "Save" button. Tapping it calls save_job, which is
        idempotent by design and moves the existing row's status back to "saved" -- so an
        application the student had recorded as "interview" is silently demoted by a button
        the UI should never have offered.
        """
        profile_id, listing_ids = seed(db, 520)
        oldest = listing_ids[0]

        found = service.saved_jobs_for_listings(db, profile_id, [oldest])
        assert oldest in found, "a save older than the 500 most recent was reported as unsaved"

        # The approach this replaced, run against the same rows, so the regression is visible
        # rather than asserted. Every caller built its lookup exactly this way.
        old_approach = {
            saved.listing_id: saved
            for saved, _listing in service.list_saved_jobs(db, profile_id, limit=500)
        }
        assert oldest not in old_approach

    def test_it_answers_for_exactly_the_listings_asked_about(self, db):
        profile_id, listing_ids = seed(db, 30)
        wanted = listing_ids[5:8]

        found = service.saved_jobs_for_listings(db, profile_id, wanted)

        assert set(found) == set(wanted)

    def test_another_students_saved_row_is_never_returned(self, db):
        mine, my_listings = seed(db, 3)
        theirs, _ = seed(db, 3)

        assert service.saved_jobs_for_listings(db, theirs, my_listings) == {}
        assert set(service.saved_jobs_for_listings(db, mine, my_listings)) == set(my_listings)

    def test_no_listings_means_no_query_and_no_rows(self, db):
        profile_id, _listing_ids = seed(db, 3)

        assert service.saved_jobs_for_listings(db, profile_id, []) == {}


class TestSavedSearchListing:
    def test_more_saved_searches_than_the_old_cap_are_all_returned(self, db):
        """Same silent-truncation shape as the tracker: there is no "load more" on the
        saved-searches screen, so anything the cap hides is hidden with nothing on screen to
        say a row exists."""
        profile_id, _listing_ids = seed(db, 1)
        for index in range(60):
            service.create_saved_search(
                db, profile_id, name=f"Search {index}", filters={"keywords": "data"}
            )

        assert len(service.list_saved_searches(db, profile_id)) == 60

    def test_the_ceiling_is_documented_and_generous(self, db):
        assert service.MAX_SAVED_SEARCHES >= 200

    def test_another_students_searches_are_not_listed(self, db):
        mine, _ = seed(db, 1)
        theirs, _ = seed(db, 1)
        service.create_saved_search(db, theirs.id if hasattr(theirs, "id") else theirs,
                                    name="Theirs", filters={})

        assert service.list_saved_searches(db, mine) == []
