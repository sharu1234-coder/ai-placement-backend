"""The account-deletion outbox: Phase 1 atomicity, and the worker's behaviour.

The invariant under test throughout is the one from
docs/proposal-account-deletion-outbox.md section 1: **never destroy the last pointer to an
object that is still stored.** Most of these tests exist to try to break it.

Run on SQLite with foreign keys enforced, which is what makes the deliberate *absence* of a
foreign key on `deletion_requests.original_user_id` testable here. The PostgreSQL-specific
behaviour -- the `FOR UPDATE SKIP LOCKED` claim under real concurrency, and the constraints
and indexes on the real engine -- is in tests/unit/test_deletion_outbox_postgres.py.
"""

import datetime
import logging

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.database.downgrade_guard import OutstandingObligations, refuse_if_outstanding
from app.models.deletion_job import DeletionJob
from app.models.deletion_request import DeletionRequest
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.user import User
from app.services import deletion_outbox
from app.services.account_service import delete_account
from app.storage.provider import StorageError, StorageProvider
from app.workers import deletion_worker

_KEY = "resumes/42/cv.pdf"
_UNRESOLVED = ("pending", "needs_attention")


class FakeStorage(StorageProvider):
    """Records what it was asked to delete. Optionally fails a set number of times.

    `delete` of an absent object is success here, because that is the contract the real
    providers implement -- both LocalStorageProvider and S3StorageProvider treat a missing
    object as done, and tests/unit/test_account_service.py already pins that.
    """

    def __init__(self, fail_times: int = 0, error: Exception | None = None) -> None:
        self.deleted: list[str] = []
        self.fail_times = fail_times
        self.error = error or StorageError("simulated storage outage")

    def save(self, key: str, data: bytes) -> str:  # pragma: no cover - not used here
        raise NotImplementedError

    def delete(self, key: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        self.deleted.append(key)

    def load(self, key: str) -> bytes:  # pragma: no cover - not used here
        raise NotImplementedError


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        # Enforced, so the absent FK on original_user_id is a real finding rather than
        # SQLite's indifference.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()


def _seed_account(db, email="leaving@example.com", keys=(_KEY,)):
    user = User(email=email, password_hash="x", is_active=True, token_version=3)
    db.add(user)
    db.flush()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.flush()
    for index, key in enumerate(keys):
        db.add(
            Resume(
                profile_id=profile.id,
                file_path=key,
                raw_text=f"resume {index}",
                parse_status="done",
            )
        )
    db.commit()
    return user, profile


class TestPhase1IsAtomic:
    def test_revocation_and_durable_jobs_land_in_the_same_commit(self, db_session):
        user, _profile = _seed_account(db_session)
        before_version = user.token_version

        delete_account(db_session, user, FakeStorage())

        request = db_session.query(DeletionRequest).one()
        job = db_session.query(DeletionJob).one()
        assert request.status == "pending"
        assert job.storage_key == _KEY
        assert job.deletion_request_id == request.id
        # Same transaction, so the revocation is visible exactly when the job is.
        refreshed = db_session.get(User, request.original_user_id)
        assert refreshed.token_version == before_version + 1
        assert refreshed.is_active is False

    def test_a_failure_before_the_commit_cannot_erase_the_last_pointer(
        self, db_session, monkeypatch
    ):
        """The case the whole design exists for.

        If Phase 1 dies after the jobs are inserted but before it commits, the rollback has to
        take the jobs *and* leave the resume row -- so the key survives somewhere. A design
        that committed the row deletes separately could lose the only pointer here.
        """
        user, _profile = _seed_account(db_session)

        def explode(*_args, **_kwargs):
            raise RuntimeError("simulated failure mid-Phase-1")

        monkeypatch.setattr(deletion_outbox, "minimise", explode)
        with pytest.raises(RuntimeError):
            delete_account(db_session, user, FakeStorage())
        db_session.rollback()

        assert db_session.query(Resume).count() == 1, "the only pointer to the object was lost"
        assert db_session.query(Resume).one().file_path == _KEY
        assert db_session.query(DeletionJob).count() == 0
        assert db_session.query(DeletionRequest).count() == 0
        assert db_session.query(User).count() == 1

    def test_phase_one_makes_no_storage_call(self, db_session):
        """Account inaccessibility must not depend on a working bucket."""
        storage = FakeStorage(fail_times=99)
        user, _profile = _seed_account(db_session)

        delete_account(db_session, user, storage)  # a bucket that fails everything

        assert storage.deleted == []
        assert db_session.query(DeletionJob).one().status == "pending"
        assert db_session.get(User, db_session.query(DeletionRequest).one().original_user_id).is_active is False

    def test_the_user_row_survives_until_the_objects_are_gone(self, db_session):
        user, _profile = _seed_account(db_session)
        user_id = user.id

        delete_account(db_session, user, FakeStorage())

        assert db_session.get(User, user_id) is not None
        assert db_session.query(StudentProfile).count() == 0
        assert db_session.query(Resume).count() == 0

    def test_an_account_with_nothing_stored_is_finished_in_phase_one(self, db_session):
        """No objects, no obligation to outlive the transaction."""
        user, _profile = _seed_account(db_session, email="no-files@example.com", keys=())
        user_id = user.id

        delete_account(db_session, user, FakeStorage())

        assert db_session.get(User, user_id) is None
        assert db_session.query(DeletionRequest).count() == 0

    def test_the_identifying_columns_are_overwritten(self, db_session):
        user, _profile = _seed_account(db_session, email="real.person@example.com")

        delete_account(db_session, user, FakeStorage())

        row = db_session.get(User, db_session.query(DeletionRequest).one().original_user_id)
        assert "real.person" not in row.email
        assert row.email.endswith("@deleted.invalid")
        assert row.password_hash == "!deleted"

    def test_the_freed_email_can_be_reused_immediately(self, db_session):
        """A tombstone must not block someone signing up again with their own address."""
        user, _profile = _seed_account(db_session, email="again@example.com")
        delete_account(db_session, user, FakeStorage())

        db_session.add(User(email="again@example.com", password_hash="y"))
        db_session.commit()  # the unique index must not fire


class TestJobCreation:
    def test_one_job_per_object(self, db_session):
        user, _profile = _seed_account(db_session, keys=("a/1.pdf", "a/2.pdf", "a/3.pdf"))
        delete_account(db_session, user, FakeStorage())
        assert db_session.query(DeletionJob).count() == 3

    def test_two_rows_sharing_one_object_make_one_job(self, db_session):
        """One object is one obligation, not two jobs racing each other."""
        user, _profile = _seed_account(db_session, keys=(_KEY, _KEY))
        delete_account(db_session, user, FakeStorage())
        assert db_session.query(DeletionJob).count() == 1

    def test_the_database_refuses_a_duplicate_job(self, db_session):
        """Idempotence of a retried Phase 1 is enforced in the schema, not just the service."""
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        job = db_session.query(DeletionJob).one()

        db_session.add(
            DeletionJob(
                deletion_request_id=job.deletion_request_id,
                storage_provider=job.storage_provider,
                storage_key=job.storage_key,
                status="pending",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_the_request_has_no_foreign_key_to_users(self, db_session):
        """Deliberate. An enforced FK would make the user row undeletable while jobs remain."""
        keys = {
            fk["constrained_columns"][0]
            for fk in inspect(db_session.bind).get_foreign_keys("deletion_requests")
        }
        assert "original_user_id" not in keys

        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        request = db_session.query(DeletionRequest).one()
        # The proof, under enforced foreign keys: the user row goes, the obligation stays.
        db_session.delete(db_session.get(User, request.original_user_id))
        db_session.commit()
        assert db_session.query(DeletionRequest).one().status == "pending"


class TestStorageOutcomes:
    def _requested(self, db):
        user, _profile = _seed_account(db)
        delete_account(db, user, FakeStorage())
        return db.query(DeletionRequest).one()

    def test_success_marks_the_job_done_and_closes_the_request(self, db_session):
        self._requested(db_session)
        storage = FakeStorage()

        report = deletion_worker.run_once(db_session, storage)

        assert storage.deleted == [_KEY]
        assert report.deleted == 1
        assert report.requests_closed == 1
        assert db_session.query(DeletionJob).one().status == "done"
        assert db_session.query(DeletionRequest).one().status == "done"

    def test_an_absent_object_is_success(self, db_session):
        """The obligation is "the object is not there", not "this process removed it"."""
        self._requested(db_session)

        class AbsentStorage(FakeStorage):
            def delete(self, key: str) -> None:
                self.deleted.append(key)  # the real providers swallow a missing object

        report = deletion_worker.run_once(db_session, AbsentStorage())
        assert report.resolved == 1
        assert db_session.query(DeletionJob).one().status == "done"

    def test_a_transient_failure_retries_and_then_succeeds(self, db_session):
        self._requested(db_session)
        storage = FakeStorage(fail_times=1)

        first = deletion_worker.run_once(db_session, storage)
        assert first.retried == 1
        job = db_session.query(DeletionJob).one()
        assert job.status == "pending"
        assert job.attempts == 1
        assert job.last_error_class == "StorageError"
        assert job.locked_until is None, "a failed job must not stay leased"
        assert db_session.query(DeletionRequest).one().status == "pending"

        # Due again once the backoff has passed.
        job.next_attempt_at = deletion_worker._now() - datetime.timedelta(seconds=1)
        db_session.commit()

        second = deletion_worker.run_once(db_session, storage)
        assert second.deleted == 1
        assert db_session.query(DeletionJob).one().status == "done"
        assert db_session.query(DeletionRequest).one().status == "done"

    def test_no_storage_key_reaches_the_log(self, db_session, caplog):
        """The key is a user-scoped path, so it is personal data by association."""
        self._requested(db_session)
        with caplog.at_level(logging.DEBUG):
            deletion_worker.run_once(db_session, FakeStorage(fail_times=1))
        assert _KEY not in caplog.text
        assert "cv.pdf" not in caplog.text

    def test_a_failure_message_is_not_logged_only_its_class(self, db_session, caplog):
        self._requested(db_session)
        secretive = FakeStorage(fail_times=1, error=StorageError(f"AccessDenied on {_KEY}"))
        with caplog.at_level(logging.DEBUG):
            deletion_worker.run_once(db_session, secretive)
        assert "AccessDenied on" not in caplog.text
        assert "StorageError" in caplog.text


class TestLeasesAndCrashes:
    def _job(self, db):
        user, _profile = _seed_account(db)
        delete_account(db, user, FakeStorage())
        return db.query(DeletionJob).one()

    def test_a_leased_job_is_not_claimed_again(self, db_session):
        self._job(db_session)
        first = deletion_worker.claim_batch(db_session)
        db_session.commit()
        assert len(first) == 1

        again = deletion_worker.claim_batch(db_session)
        assert again == [], "a job under a live lease must not be claimable"

    def test_an_expired_lease_is_reclaimable(self, db_session):
        """What makes a crash cost a lease rather than a job -- nobody has to notice it."""
        job = self._job(db_session)
        deletion_worker.claim_batch(db_session)
        db_session.commit()

        job.locked_until = deletion_worker._now() - datetime.timedelta(seconds=1)
        db_session.commit()

        assert len(deletion_worker.claim_batch(db_session)) == 1

    def test_a_crash_between_the_delete_and_the_status_write_is_safe(self, db_session):
        """The dangerous window, walked through deliberately.

        The object is gone but the job still says pending. The retry deletes an object that is
        already absent, which is success -- which is exactly why idempotence is required
        rather than merely desirable.
        """
        job = self._job(db_session)
        storage = FakeStorage()

        deletion_worker.claim_batch(db_session)
        db_session.commit()
        storage.delete(job.storage_key)  # the call succeeded...
        db_session.rollback()            # ...and then the process died

        assert db_session.query(DeletionJob).one().status == "pending"
        job.locked_until = deletion_worker._now() - datetime.timedelta(seconds=1)
        db_session.commit()

        report = deletion_worker.run_once(db_session, storage)
        assert report.deleted == 1
        assert storage.deleted == [_KEY, _KEY], "the retry must re-attempt, harmlessly"
        assert db_session.query(DeletionRequest).one().status == "done"

    def test_a_job_not_yet_due_is_not_claimed(self, db_session):
        job = self._job(db_session)
        job.next_attempt_at = deletion_worker._now() + datetime.timedelta(minutes=10)
        db_session.commit()
        assert deletion_worker.claim_batch(db_session) == []


class TestRetryExhaustionEscalatesWithoutAbandoning:
    def _job(self, db):
        user, _profile = _seed_account(db)
        delete_account(db, user, FakeStorage())
        return db.query(DeletionJob).one()

    def test_it_becomes_needs_attention_but_stays_queued(self, db_session, caplog):
        job = self._job(db_session)
        storage = FakeStorage(fail_times=99)

        for _ in range(deletion_worker.ATTENTION_AFTER_ATTEMPTS):
            job.next_attempt_at = deletion_worker._now() - datetime.timedelta(seconds=1)
            job.locked_until = None
            db_session.commit()
            with caplog.at_level(logging.ERROR):
                deletion_worker.run_once(db_session, storage)

        job = db_session.query(DeletionJob).one()
        assert job.status == "needs_attention"
        assert job.attempts >= deletion_worker.ATTENTION_AFTER_ATTEMPTS
        assert job.status != "failed", "a terminal failure would abandon the erasure"
        assert db_session.query(DeletionRequest).one().status == "pending"
        assert "keep retrying" in caplog.text

    def test_a_needs_attention_job_is_still_claimed(self, db_session):
        """`needs_attention` is a flag for operators, not an exit from the queue."""
        job = self._job(db_session)
        job.status = "needs_attention"
        job.next_attempt_at = deletion_worker._now() - datetime.timedelta(seconds=1)
        db_session.commit()

        assert len(deletion_worker.claim_batch(db_session)) == 1

    def test_it_still_completes_once_storage_recovers(self, db_session):
        job = self._job(db_session)
        job.status = "needs_attention"
        job.attempts = 20
        job.next_attempt_at = deletion_worker._now() - datetime.timedelta(seconds=1)
        db_session.commit()

        report = deletion_worker.run_once(db_session, FakeStorage())
        assert report.deleted == 1
        assert db_session.query(DeletionRequest).one().status == "done"

    def test_the_backoff_is_capped(self, db_session):
        """Unbounded backoff is how an obligation goes quiet."""
        assert deletion_worker.backoff_for(0) == deletion_worker.BACKOFF_BASE
        assert deletion_worker.backoff_for(3) > deletion_worker.BACKOFF_BASE
        assert deletion_worker.backoff_for(50) == deletion_worker.BACKOFF_CAP
        assert deletion_worker.backoff_for(500) == deletion_worker.BACKOFF_CAP

    def test_there_is_no_status_that_stops_retrying(self):
        from app.models.deletion_job import UNRESOLVED_STATUSES

        assert "needs_attention" in UNRESOLVED_STATUSES
        assert not any("fail" in status for status in UNRESOLVED_STATUSES)


class TestCompletion:
    def test_the_user_row_goes_only_after_the_last_job(self, db_session):
        user, _profile = _seed_account(db_session, keys=("a/1.pdf", "a/2.pdf"))
        delete_account(db_session, user, FakeStorage())
        user_id = db_session.query(DeletionRequest).one().original_user_id

        # One object erased, one still pending.
        first = db_session.query(DeletionJob).order_by(DeletionJob.id).first()
        first.status = "done"
        first.completed_at = deletion_worker._now()
        db_session.commit()
        assert deletion_worker.close_request_if_finished(db_session, first.deletion_request_id) is False
        db_session.commit()
        assert db_session.get(User, user_id) is not None

        deletion_worker.run_once(db_session, FakeStorage())
        assert db_session.get(User, user_id) is None
        assert db_session.query(DeletionRequest).one().status == "done"

    def test_completion_never_touches_storage(self, db_session):
        """So a bucket outage cannot keep the user row alive once the objects are gone."""
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        user_id = db_session.query(DeletionRequest).one().original_user_id
        job = db_session.query(DeletionJob).one()
        job.status = "done"
        db_session.commit()

        exploding = FakeStorage(fail_times=99)
        assert deletion_worker.close_request_if_finished(db_session, job.deletion_request_id) is True
        db_session.commit()
        assert db_session.get(User, user_id) is None
        assert exploding.deleted == []

    def test_closing_twice_is_harmless(self, db_session):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        deletion_worker.run_once(db_session, FakeStorage())
        request_id = db_session.query(DeletionRequest).one().id

        assert deletion_worker.close_request_if_finished(db_session, request_id) is False
        assert db_session.query(DeletionRequest).one().status == "done"

    def test_a_whole_run_repeated_produces_one_outcome(self, db_session):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        storage = FakeStorage()

        deletion_worker.run_once(db_session, storage)
        second = deletion_worker.run_once(db_session, storage)

        assert storage.deleted == [_KEY]
        assert second.claimed == 0
        assert db_session.query(DeletionRequest).count() == 1


class TestTheProvisionalSlaTargets:
    def test_the_values_match_the_approved_design(self):
        assert deletion_worker.PICKUP_TARGET == datetime.timedelta(minutes=5)
        assert deletion_worker.ALERT_AFTER == datetime.timedelta(hours=1)
        assert deletion_worker.ESCALATE_AFTER == datetime.timedelta(hours=24)
        assert deletion_worker.LEASE == deletion_worker.PICKUP_TARGET

    def test_they_are_labelled_pending_privacy_and_legal_review(self):
        import inspect as py_inspect

        source = py_inspect.getsource(deletion_worker)
        assert "pending privacy and legal review" in source
        assert "provisional" in source

    def test_an_hour_old_request_alerts(self, db_session, caplog):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        request = db_session.query(DeletionRequest).one()

        later = deletion_worker._aware(request.created_at) + datetime.timedelta(hours=2)
        with caplog.at_level(logging.ERROR):
            breaches = deletion_worker.report_sla_breaches(db_session, now=later)
        assert breaches["alert"] == [request.request_uuid]
        assert "1-hour alert target" in caplog.text
        assert "pending privacy/legal review" in caplog.text

    def test_a_day_old_request_escalates_instead(self, db_session, caplog):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        request = db_session.query(DeletionRequest).one()

        later = deletion_worker._aware(request.created_at) + datetime.timedelta(hours=30)
        with caplog.at_level(logging.ERROR):
            breaches = deletion_worker.report_sla_breaches(db_session, now=later)
        assert breaches["escalate"] == [request.request_uuid]
        assert breaches["alert"] == [], "a request is reported once, at its worst threshold"
        assert "24-hour escalation target" in caplog.text

    def test_the_clock_starts_at_revocation_not_at_pickup(self, db_session):
        """created_at is set in the Phase 1 transaction, which is when access was revoked."""
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        request = db_session.query(DeletionRequest).one()
        assert request.created_at is not None
        assert deletion_worker.overdue_requests(
            db_session,
            threshold=datetime.timedelta(0),
            now=deletion_worker._aware(request.created_at),
        )

    def test_a_closed_request_is_never_reported(self, db_session):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        deletion_worker.run_once(db_session, FakeStorage())
        far_future = deletion_worker._now() + datetime.timedelta(days=30)
        assert deletion_worker.report_sla_breaches(db_session, now=far_future) == {
            "alert": [],
            "escalate": [],
        }

    def test_the_pickup_target_is_observable(self, db_session):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        assert deletion_worker.claimable_count(db_session) == 1
        deletion_worker.run_once(db_session, FakeStorage())
        assert deletion_worker.claimable_count(db_session) == 0


class TestTheDowngradeIsRefusedWhileObligationsRemain:
    def test_an_open_request_blocks_it(self, db_session):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())

        with pytest.raises(OutstandingObligations) as exc:
            refuse_if_outstanding(
                db_session.connection(),
                table="deletion_requests",
                unresolved=_UNRESOLVED,
                obligation="account erasure request",
            )
        assert "1" in str(exc.value)

    def test_needs_attention_also_blocks_it(self, db_session):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        db_session.query(DeletionRequest).one().status = "needs_attention"
        db_session.commit()

        with pytest.raises(OutstandingObligations):
            refuse_if_outstanding(
                db_session.connection(),
                table="deletion_requests",
                unresolved=_UNRESOLVED,
                obligation="account erasure request",
            )

    def test_a_drained_queue_allows_it(self, db_session):
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        deletion_worker.run_once(db_session, FakeStorage())

        refuse_if_outstanding(
            db_session.connection(),
            table="deletion_requests",
            unresolved=_UNRESOLVED,
            obligation="account erasure request",
        )

    def test_the_message_names_no_person(self, db_session):
        user, _profile = _seed_account(db_session, email="someone@example.com")
        delete_account(db_session, user, FakeStorage())
        with pytest.raises(OutstandingObligations) as exc:
            refuse_if_outstanding(
                db_session.connection(),
                table="deletion_requests",
                unresolved=_UNRESOLVED,
                obligation="account erasure request",
            )
        assert "someone@example.com" not in str(exc.value)
        assert _KEY not in str(exc.value)

    def test_the_migration_offers_no_override(self):
        """A switch that let the drop proceed anyway is how an obligation disappears."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "alembic/versions/f7b3c91de204_add_account_deletion_outbox.py"
        ).read_text(encoding="utf-8")
        assert "refuse_if_outstanding" in source
        assert "os.environ" not in source
        assert "getenv" not in source

    def test_the_migration_documents_the_absent_foreign_key(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "alembic/versions/f7b3c91de204_add_account_deletion_outbox.py"
        ).read_text(encoding="utf-8")
        assert "DELIBERATELY NO FOREIGN KEY" in source


class TestNoErasureReceiptIsKeptByDefault:
    def test_a_completed_request_holds_nothing_identifying(self, db_session):
        user, _profile = _seed_account(db_session, email="gone@example.com")
        delete_account(db_session, user, FakeStorage())
        deletion_worker.run_once(db_session, FakeStorage())

        request = db_session.query(DeletionRequest).one()
        columns = {c.name for c in DeletionRequest.__table__.columns}
        assert columns == {
            "id",
            "request_uuid",
            "original_user_id",
            "status",
            "created_at",
            "updated_at",
            "completed_at",
        }, "a new column here would be an erasure receipt by the back door"
        assert "gone@example.com" not in str(request.__dict__)

    def test_the_key_is_gone_once_the_job_is_done(self, db_session):
        """The job row keeps the key only while it is still needed to erase the object."""
        user, _profile = _seed_account(db_session)
        delete_account(db_session, user, FakeStorage())
        deletion_worker.run_once(db_session, FakeStorage())
        job = db_session.query(DeletionJob).one()
        # Retained with the closed request, which section 11.3 covers; flagged here so that a
        # decision to purge resolved jobs has a test waiting for it.
        assert job.status == "done"
        assert job.completed_at is not None


class NamedStorage(FakeStorage):
    """A fake that declares a backend name, like both real adapters do."""

    def __init__(self, name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.name = name


class TestTheWrongBackendIsRefused:
    """A job must never be erased against a backend it was not enqueued for.

    This is the one failure mode the outbox cannot absorb, because it does not look like a
    failure. An object key is only meaningful to the backend that stored it, so deleting it
    from a different one almost always *succeeds*: the key is absent there, and absence is
    idempotent success. The job would be marked `done`, the request would close, and closing
    the request deletes the last row that still held the key -- leaving the object exactly
    where it was, now unreachable, for a user who was told it was erased.

    The route into it is ordinary: `STORAGE_PROVIDER` is recorded on the job at enqueue time,
    and the production startup guard in app/core/config.py actively pushes a deployment to
    change that setting from `local` to `s3`. Any job enqueued before the change is a job whose
    recorded backend no longer matches the running worker's.
    """

    def _requested(self, db, provider: str = "local"):
        user, _profile = _seed_account(db)
        delete_account(db, user, FakeStorage())
        job = db.query(DeletionJob).one()
        job.storage_provider = provider
        db.commit()
        return db.query(DeletionRequest).one()

    def test_a_mismatched_provider_deletes_nothing_and_keeps_the_obligation(self, db_session):
        self._requested(db_session, provider="local")
        storage = NamedStorage("s3")

        report = deletion_worker.run_once(db_session, storage)

        assert storage.deleted == [], "nothing may be deleted against the wrong backend"
        assert report.deleted == 0
        assert report.needs_attention == 1
        assert report.requests_closed == 0

        job = db_session.query(DeletionJob).one()
        assert job.status == "needs_attention"
        assert job.completed_at is None
        assert job.last_error_class == "ProviderMismatch"
        assert job.locked_until is None, "a refused job must not stay leased"
        # The two things that must survive: the key, and the row that holds it.
        assert job.storage_key == _KEY
        assert job.storage_provider == "local"
        assert db_session.query(DeletionRequest).one().status == "pending"
        assert db_session.query(User).count() == 1, "the last user row is the only pointer left"

    def test_it_is_flagged_on_the_first_attempt_rather_than_after_five(self, db_session):
        """A mismatch is a configuration fault. Retrying it cannot change the outcome, so
        making an operator wait out five exponential backoffs only delays the only thing that
        resolves it."""
        self._requested(db_session, provider="local")

        deletion_worker.run_once(db_session, NamedStorage("s3"))

        job = db_session.query(DeletionJob).one()
        assert job.attempts == 1
        assert job.status == "needs_attention"
        assert job.attempts < deletion_worker.ATTENTION_AFTER_ATTEMPTS

    def test_the_job_completes_once_a_worker_holds_the_right_backend(self, db_session):
        """`needs_attention` is a flag, not an exit: the job stays claimable, so correcting
        the configuration is the whole remedy -- nothing has to be requeued by hand."""
        self._requested(db_session, provider="local")
        deletion_worker.run_once(db_session, NamedStorage("s3"))

        job = db_session.query(DeletionJob).one()
        job.next_attempt_at = deletion_worker._now() - datetime.timedelta(seconds=1)
        db_session.commit()

        correct = NamedStorage("local")
        report = deletion_worker.run_once(db_session, correct)

        assert correct.deleted == [_KEY]
        assert report.deleted == 1
        assert db_session.query(DeletionJob).one().status == "done"
        assert db_session.query(DeletionRequest).one().status == "done"

    def test_the_refusal_names_the_backends_but_never_the_key(self, db_session, caplog):
        self._requested(db_session, provider="local")

        with caplog.at_level(logging.ERROR, logger="app.workers.deletion_worker"):
            deletion_worker.run_once(db_session, NamedStorage("s3"))

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "local" in text and "s3" in text
        assert _KEY not in text
        assert "cv.pdf" not in text

    def test_a_provider_without_a_declared_name_is_not_second_guessed(self, db_session):
        """The comparison engages only when the provider says what it is. A bare fake, and any
        provider predating the attribute, keeps the previous behaviour rather than having every
        job refused; tests/unit/test_storage.py pins that both real adapters declare a name."""
        self._requested(db_session, provider="local")

        report = deletion_worker.run_once(db_session, FakeStorage())

        assert report.deleted == 1
        assert db_session.query(DeletionJob).one().status == "done"


class TestFailureCodesAreRecordedForTriage:
    def _requested(self, db):
        user, _profile = _seed_account(db)
        delete_account(db, user, FakeStorage())
        return db.query(DeletionRequest).one()

    def test_a_backend_error_code_is_kept_beside_the_class(self, db_session):
        """Without this, `last_error_class` is the constant string "StorageError" for every
        failure either adapter can produce, because both wrap everything in that one class.
        An operator triaging a stuck erasure could not tell a denied permission, which needs a
        human, from throttling, which does not."""
        self._requested(db_session)
        storage = FakeStorage(fail_times=1, error=StorageError("denied", code="AccessDenied"))

        deletion_worker.run_once(db_session, storage)

        assert db_session.query(DeletionJob).one().last_error_class == "StorageError:AccessDenied"

    def test_an_uncoded_failure_still_records_the_class_alone(self, db_session):
        self._requested(db_session)

        deletion_worker.run_once(db_session, FakeStorage(fail_times=1))

        assert db_session.query(DeletionJob).one().last_error_class == "StorageError"

    def test_the_recorded_code_cannot_carry_the_key_or_the_bucket(self, db_session):
        """The code is a short machine token by contract. This asserts the stored value stays
        one, so that a provider message -- which quotes the bucket and the key -- can never
        reach the column through it."""
        self._requested(db_session)
        storage = FakeStorage(
            fail_times=1,
            error=StorageError(f"could not delete {_KEY} from bucket prod-talviora", code="SlowDown"),
        )

        deletion_worker.run_once(db_session, storage)

        recorded = db_session.query(DeletionJob).one().last_error_class
        assert recorded == "StorageError:SlowDown"
        assert _KEY not in recorded
        assert "prod-talviora" not in recorded
        assert len(recorded) <= 100, "must fit DeletionJob.last_error_class"
