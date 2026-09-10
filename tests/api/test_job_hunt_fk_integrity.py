"""Job Hunt deletions, with foreign keys actually enforced.

Every other test in this suite runs against SQLite, and SQLite ignores foreign keys unless
`PRAGMA foreign_keys=ON` is set per connection -- which nothing in the app or in conftest does.
So the whole suite has been silently unable to see a class of bug that production PostgreSQL
enforces on every statement: deleting a row that something still points at.

It found a real one. `job_saved_searches` is referenced by two tables:

    saved_jobs.source_saved_search_id   -- which search a tracked job was found through
    job_alerts.saved_search_id          -- which search an alert watches

and deleting a saved search left both dangling. On SQLite that silently succeeded; on
PostgreSQL it raises, so a student tapping "delete" on a search they had an alert for -- the
most ordinary thing to have an alert for -- got a 500.

This module therefore builds its own engine with the pragma on. It is the only place in the
suite where a delete is tested against the constraints production actually has, so any new
Job Hunt deletion path belongs here too.
"""

import datetime
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- imports every model so create_all sees the full schema
from app.database.base import Base
from app.models.job_alert import JobAlert, JobAlertDelivery
from app.models.job_listing import JobListing
from app.models.job_saved_search import JobSavedSearch
from app.models.profile import StudentProfile
from app.models.saved_job import SavedJob
from app.models.user import User
from app.services.job_hunt import alerts as job_alerts
from app.services.job_hunt import service

NOW = datetime.datetime(2026, 9, 9, 12, 0, 0)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fk.db'}")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def make_profile(db) -> StudentProfile:
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    return profile


def make_listing(db, job_id: str = "1") -> JobListing:
    listing = JobListing(
        provider="fake",
        provider_job_id=job_id,
        url=f"https://jobs.example.com/{job_id}",
        title="Data Analyst",
        fetched_at=NOW,
    )
    db.add(listing)
    db.commit()
    return listing


def test_the_pragma_is_actually_on(db):
    """Guards the guard. If this fixture ever stops enforcing foreign keys, every other test
    in this module would keep passing while testing nothing."""
    listing = make_listing(db)
    db.add(SavedJob(profile_id=999_999, listing_id=listing.id, status="saved"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


class TestDeletingASavedSearch:
    def test_a_search_with_a_tracked_job_and_an_alert_can_be_deleted(self, db):
        """The bug, end to end. This raised IntegrityError before the fix."""
        profile = make_profile(db)
        listing = make_listing(db)
        search = service.create_saved_search(
            db, profile.id, name="Dubai Data Analyst", filters={"keywords": "data analyst"}
        )
        service.save_job(
            db,
            profile.id,
            listing.id,
            "applied",
            source="saved_search",
            source_saved_search_id=search.id,
        )
        alert = job_alerts.create_alert(
            db, profile.id, name="Dubai Data Analyst", saved_search_id=search.id
        )
        db.add(
            JobAlertDelivery(
                alert_id=alert.id, profile_id=profile.id, listing_id=listing.id
            )
        )
        db.commit()

        service.delete_saved_search(db, profile.id, search.id)

        assert db.query(JobSavedSearch).count() == 0

    def test_the_tracked_job_survives_and_keeps_saying_it_came_from_a_search(self, db):
        """Provenance is history, not a pointer.

        The student really did find this job through a search. Deleting the search must not
        rewrite the tracker to claim they saved it by hand -- only the search's *name* is lost,
        which the tracker already renders as a plain "Saved search".
        """
        profile = make_profile(db)
        listing = make_listing(db)
        search = service.create_saved_search(db, profile.id, name="Dubai", filters={})
        saved = service.save_job(
            db,
            profile.id,
            listing.id,
            "applied",
            source="saved_search",
            source_saved_search_id=search.id,
        )
        saved_id = saved.id

        service.delete_saved_search(db, profile.id, search.id)

        survivor = db.get(SavedJob, saved_id)
        assert survivor is not None, "deleting a search must not delete the applications"
        assert survivor.status == "applied"
        assert survivor.source == "saved_search"
        assert survivor.source_saved_search_id is None

    def test_an_alert_on_the_search_goes_with_it_rather_than_being_left_unrunnable(self, db):
        """An alert bound to a search reads its filters live from that search. Left behind, it
        would have nothing to search on and would report "not configured" forever -- a broken
        row in the student's alert list that they did not create and cannot repair."""
        profile = make_profile(db)
        listing = make_listing(db)
        search = service.create_saved_search(db, profile.id, name="Dubai", filters={})
        alert = job_alerts.create_alert(
            db, profile.id, name="Dubai", saved_search_id=search.id
        )
        db.add(
            JobAlertDelivery(
                alert_id=alert.id, profile_id=profile.id, listing_id=listing.id
            )
        )
        db.commit()

        service.delete_saved_search(db, profile.id, search.id)

        assert db.query(JobAlert).count() == 0
        assert db.query(JobAlertDelivery).count() == 0, "history for a deleted alert"

    def test_a_standalone_alert_is_untouched(self, db):
        """Only alerts bound to *this* search go. An alert carrying its own filters has no
        relationship to it and must survive."""
        profile = make_profile(db)
        search = service.create_saved_search(db, profile.id, name="Dubai", filters={})
        standalone = job_alerts.create_alert(
            db, profile.id, name="Standalone", filters={"keywords": "python"}
        )

        service.delete_saved_search(db, profile.id, search.id)

        assert db.get(JobAlert, standalone.id) is not None

    def test_another_students_alert_on_their_own_search_is_untouched(self, db):
        """The delete is scoped by profile as well as by search id."""
        mine = make_profile(db)
        theirs = make_profile(db)
        my_search = service.create_saved_search(db, mine.id, name="Dubai", filters={})
        their_search = service.create_saved_search(db, theirs.id, name="Dubai", filters={})
        their_alert = job_alerts.create_alert(
            db, theirs.id, name="Dubai", saved_search_id=their_search.id
        )

        service.delete_saved_search(db, mine.id, my_search.id)

        assert db.get(JobAlert, their_alert.id) is not None
        assert db.get(JobSavedSearch, their_search.id) is not None


class TestDeletingAnAlert:
    def test_an_alert_with_history_can_be_deleted(self, db):
        profile = make_profile(db)
        listing = make_listing(db)
        alert = job_alerts.create_alert(
            db, profile.id, name="Dubai", filters={"keywords": "data"}
        )
        db.add(
            JobAlertDelivery(
                alert_id=alert.id, profile_id=profile.id, listing_id=listing.id
            )
        )
        db.commit()

        job_alerts.delete_alert(db, profile.id, alert.id)

        assert db.query(JobAlert).count() == 0
        assert db.query(JobAlertDelivery).count() == 0


class TestAccountDeletion:
    def test_a_full_job_hunt_footprint_can_be_deleted_with_foreign_keys_enforced(self, db):
        """Account deletion is the one path where a foreign-key failure is not just a 500.

        account_service deletes Job Hunt's five profile-owned tables in a hand-chosen order
        that satisfies real foreign keys, and every test that covers it runs with those keys
        switched off -- so the ordering has never actually been checked against the constraints
        it was written for. A student asking to be deleted and getting an error instead is a
        privacy failure, not an inconvenience.

        Every Job Hunt table that carries a profile_id is populated first, so the order is
        exercised rather than skipped past on empty tables.
        """
        from app.services import account_service
        from app.storage.provider import StorageProvider

        profile = make_profile(db)
        listing = make_listing(db)
        search = service.create_saved_search(db, profile.id, name="Dubai", filters={})
        service.save_job(
            db,
            profile.id,
            listing.id,
            "applied",
            source="saved_search",
            source_saved_search_id=search.id,
        )
        alert = job_alerts.create_alert(
            db, profile.id, name="Dubai", saved_search_id=search.id
        )
        db.add(
            JobAlertDelivery(
                alert_id=alert.id, profile_id=profile.id, listing_id=listing.id
            )
        )
        search_profile = service.get_or_create_search_profile(db, profile.id)
        search_profile.target_role_title = "Data Analyst"
        db.commit()
        service._upsert_match(db, search_profile, listing)
        db.commit()

        class NullStorage(StorageProvider):
            def save(self, *args, **kwargs):  # pragma: no cover -- nothing is stored here
                raise NotImplementedError

            def delete(self, *args, **kwargs):
                return None

            def load(self, *args, **kwargs):  # pragma: no cover
                raise NotImplementedError

        user = db.get(User, profile.user_id)
        account_service.delete_account(db, user, NullStorage())

        assert db.query(JobSavedSearch).count() == 0
        assert db.query(JobAlert).count() == 0
        assert db.query(JobAlertDelivery).count() == 0
        assert db.query(SavedJob).count() == 0
        # The shared listing is deliberately kept: it is public provider content that other
        # students' saved rows point at.
        assert db.query(JobListing).count() == 1


class TestDeletingAResumeLinkedToJobHunt:
    """job_search_profiles.resume_id is a foreign key into `resumes`, added by Job Hunt after
    resume_service.delete_resume was written -- and not added to the list of FKs that function
    clears first.

    Its own docstring describes the consequence exactly: the commit raises *after* the file is
    already gone, the transaction rolls back so the Resume row survives with a file_path
    pointing at nothing, and every retry fails identically. A permanently undeletable resume,
    reached by a student doing the most ordinary thing -- linking a resume to Job Hunt and
    later deleting it -- and invisible to every test because SQLite does not enforce the key.
    """

    def _resume(self, db, profile):
        from app.models.resume import Resume

        resume = Resume(
            profile_id=profile.id,
            file_path="cv.pdf",
            original_filename="cv.pdf",
            raw_text="Python SQL",
        )
        db.add(resume)
        db.commit()
        return resume

    class _NullStorage:
        def delete(self, *args, **kwargs):
            return None

    def test_a_resume_linked_to_a_job_hunt_profile_can_still_be_deleted(self, db):
        from app.models.resume import Resume
        from app.services import resume_service

        profile = make_profile(db)
        resume = self._resume(db, profile)
        service.update_search_profile(db, profile.id, resume_id=resume.id)

        assert resume_service.delete_resume(db, resume.id, profile.id, self._NullStorage())

        assert db.query(Resume).count() == 0

    def test_the_job_hunt_setup_survives_and_stops_claiming_a_resume_it_no_longer_has(self, db):
        """The student's target role, location and declared skills are their own data. Losing
        the Job Hunt profile because a resume was cleaned up would be the worse bug -- the same
        reasoning delete_resume already applies to PlacementPath."""
        from app.services import resume_service

        profile = make_profile(db)
        resume = self._resume(db, profile)
        service.update_search_profile(
            db,
            profile.id,
            resume_id=resume.id,
            target_role_title="Data Analyst",
            preferred_location="Bengaluru",
        )

        resume_service.delete_resume(db, resume.id, profile.id, self._NullStorage())

        search_profile = service.get_or_create_search_profile(db, profile.id)
        assert search_profile.resume_id is None
        assert search_profile.target_role_title == "Data Analyst"
        assert search_profile.preferred_location == "Bengaluru"
        assert "resume_skills" not in (search_profile.signal_provenance or {}), (
            "the profile must not go on claiming a signal came from a deleted resume"
        )

    def test_another_students_job_hunt_profile_is_untouched(self, db):
        from app.services import resume_service

        mine = make_profile(db)
        theirs = make_profile(db)
        my_resume = self._resume(db, mine)
        their_resume = self._resume(db, theirs)
        service.update_search_profile(db, mine.id, resume_id=my_resume.id)
        service.update_search_profile(db, theirs.id, resume_id=their_resume.id)

        resume_service.delete_resume(db, my_resume.id, mine.id, self._NullStorage())

        assert service.get_or_create_search_profile(db, theirs.id).resume_id == their_resume.id
