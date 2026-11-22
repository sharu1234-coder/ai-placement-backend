"""What the worker does with each way an erasure can fail to finish.

Stage A proved that an unverified *destination* reaches neither storage nor `done`. This is the
same question one step later: given that the destination is confirmed and the erasure was
attempted, which outcomes may close the job and which must hold it.

The rule every test here checks one face of: a job reaches `done` only from a verified erasure.
Everything else stays unresolved and claimable, because the obligation outlives the attempt --
and closing a request deletes the last row holding the object key, so a premature `done` is not
a retryable mistake but a permanent one.

The second thing being tested is the *reason*. An operator triages on `last_error_class`, and
the response to a legal hold (honour it) is the opposite of the response to a throttle (wait)
and to a denied permission (change a policy). A failure recorded under the wrong reason sends
somebody after the wrong thing, so each class is asserted rather than assumed.

SQLite: this is routing logic over a provider's reported outcome. The engine-specific
concurrency behaviour stays in tests/unit/test_deletion_outbox_postgres.py.
"""

import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.deletion_job import DeletionJob
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.user import User
from app.services.account_service import delete_account
from app.storage import erasure
from app.storage.erasure import (
    VERIFIED_BY_ABSENCE_WITHOUT_HISTORY,
    VERSIONING_NONE,
    ErasureOutcome,
)
from app.storage.identity import StorageIdentity
from app.storage.provider import StorageError, StorageProvider
from app.workers import deletion_worker

_KEY = "resumes/42/cv.pdf"


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()


class Erasing(StorageProvider):
    """A local-identity provider whose `erase` can be made to do anything.

    `name` and `identity()` are declared because the worker confirms a destination before it
    will erase: a double that cannot say where it points is refused before any of the routing
    under test here is reached.
    """

    name = "local"

    def __init__(self, error=None, outcome=None):
        self.erased: list[str] = []
        self.error = error
        self._outcome = outcome or ErasureOutcome(
            versioning=VERSIONING_NONE, verified_by=VERIFIED_BY_ABSENCE_WITHOUT_HISTORY
        )

    def identity(self):
        return StorageIdentity(provider=self.name)

    def erase(self, key):
        if self.error is not None:
            raise self.error
        self.erased.append(key)
        return self._outcome

    def save(self, key, data):  # pragma: no cover - not exercised
        return key

    def delete(self, key):  # pragma: no cover - the worker finishes through erase()
        raise AssertionError("the worker must not finish a deletion job through delete()")

    def load(self, key):  # pragma: no cover - not exercised
        raise NotImplementedError


class CannotErase:
    """Duck-typed, with no `erase` at all.

    A real adapter cannot be in this state -- `StorageProvider` supplies a refusing default --
    but a non-conforming object can, and an AttributeError raised out of `execute_job` would
    abandon the rest of the claimed batch to its leases rather than holding one job.
    """

    name = "local"

    def identity(self):
        return StorageIdentity(provider="local")

    def delete(self, key):  # pragma: no cover - must never be reached
        raise AssertionError("a provider that cannot erase must not fall back to delete()")


def _one_job(db):
    user = User(email="leaving@example.com", password_hash="x", is_active=True, token_version=3)
    db.add(user)
    db.flush()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.flush()
    db.add(Resume(profile_id=profile.id, file_path=_KEY, raw_text="r", parse_status="done"))
    db.commit()
    delete_account(db, user, Erasing())
    return db.query(DeletionJob).one()


def _assert_unresolved(job):
    """The shared half of every refusal: still owed, still claimable, no lease held."""
    assert job.status in deletion_worker.UNRESOLVED_STATUSES
    assert job.completed_at is None, "an unfinished erasure must not look completed"
    assert job.locked_until is None, "a job not being worked on must not hold a lease"
    assert job.next_attempt_at is not None, "it has to come back"


class TestAVerifiedErasureCompletesTheJob:
    def test_it_is_marked_done_and_its_error_cleared(self, db_session):
        job = _one_job(db_session)
        storage = Erasing()

        report = deletion_worker.run_once(db_session, storage)
        db_session.refresh(job)

        assert storage.erased == [_KEY]
        assert job.status == "done"
        assert job.completed_at is not None
        assert job.last_error_class is None
        assert report.deleted == 1

    def test_the_worker_finishes_through_erase_and_never_through_delete(self, db_session):
        """`delete` on the double raises if it is called. On a versioned bucket that call is
        the defect: it inserts a delete marker and erases nothing."""
        _one_job(db_session)

        deletion_worker.run_once(db_session, Erasing())


class TestSurvivingVersionsNeverComplete:
    def test_versions_that_remain_hold_the_job_and_retry(self, db_session):
        """Retried rather than escalated: a version arriving between the delete and the
        verification is what a concurrent writer looks like, and the next pass erases it."""
        job = _one_job(db_session)
        storage = Erasing(
            error=StorageError("still there", code=erasure.ERASURE_VERSIONS_REMAIN)
        )

        deletion_worker.run_once(db_session, storage)
        db_session.refresh(job)

        assert job.status == "pending", "a survivable condition must not be escalated at once"
        assert job.last_error_class == f"StorageError:{erasure.ERASURE_VERSIONS_REMAIN}"
        _assert_unresolved(job)

    def test_it_is_escalated_once_it_stops_looking_transient(self, db_session):
        """No retry ceiling closes the obligation; the cap makes it visible, not terminal."""
        job = _one_job(db_session)
        storage = Erasing(
            error=StorageError("still there", code=erasure.ERASURE_VERSIONS_REMAIN)
        )

        for _ in range(deletion_worker.ATTENTION_AFTER_ATTEMPTS):
            job.next_attempt_at = datetime.datetime.now(
                datetime.timezone.utc
            ) - datetime.timedelta(minutes=1)
            db_session.commit()
            deletion_worker.run_once(db_session, storage)

        db_session.refresh(job)
        assert job.status == "needs_attention"
        _assert_unresolved(job)


class TestProtectedVersionsAreHeldForAPerson:
    @pytest.mark.parametrize(
        "code",
        [erasure.ERASURE_OBJECT_LOCKED, erasure.ERASURE_LEGAL_HOLD],
    )
    def test_a_lock_is_escalated_at_once_and_never_bypassed(self, db_session, code):
        """Honouring the lock is the point of it. Retrying on a 30-second backoff would only
        delay the moment somebody is told, and a bypass is never attempted anywhere."""
        job = _one_job(db_session)

        deletion_worker.run_once(db_session, Erasing(error=StorageError("locked", code=code)))
        db_session.refresh(job)

        assert job.status == "needs_attention"
        assert job.attempts == 1, "a lock needs a person on the first attempt, not the fifth"
        assert job.last_error_class == f"StorageError:{code}"
        _assert_unresolved(job)

    def test_a_locked_object_does_not_close_its_request(self, db_session):
        """Closing the request deletes the last row holding the key. While a version is still
        stored under a lock, that row is the only thing that knows where it is."""
        job = _one_job(db_session)
        request_id = job.deletion_request_id

        deletion_worker.run_once(
            db_session, Erasing(error=StorageError("held", code=erasure.ERASURE_LEGAL_HOLD))
        )

        from app.models.deletion_request import DeletionRequest

        assert db_session.get(DeletionRequest, request_id).status != "done"
        assert db_session.query(DeletionJob).one().storage_key == _KEY


class TestConfigurationAndAuthorizationFailures:
    @pytest.mark.parametrize(
        "code",
        [
            "NoSuchBucket",
            erasure.ERASURE_VERSIONING_UNKNOWN,
            erasure.ERASURE_INVENTORY_UNSUPPORTED,
            erasure.ERASURE_NOT_IMPLEMENTED,
            "AccessDenied",
            "AllAccessDisabled",
            "MethodNotAllowed",
        ],
    )
    def test_each_is_escalated_on_the_first_attempt(self, db_session, code):
        """None of these clears by being retried: a bucket does not appear, a service does not
        start implementing version listing, and a policy does not change on its own."""
        job = _one_job(db_session)

        deletion_worker.run_once(db_session, Erasing(error=StorageError("no", code=code)))
        db_session.refresh(job)

        assert job.status == "needs_attention"
        assert job.attempts == 1
        assert job.last_error_class == f"StorageError:{code}"
        _assert_unresolved(job)

    @pytest.mark.parametrize("code", ["SlowDown", "ServiceUnavailable", "InternalError", "503"])
    def test_a_transient_code_is_retried_instead(self, db_session, code):
        """The distinction that makes immediate escalation safe: these do clear by themselves,
        and escalating them would make `needs_attention` mean nothing."""
        job = _one_job(db_session)

        deletion_worker.run_once(db_session, Erasing(error=StorageError("later", code=code)))
        db_session.refresh(job)

        assert job.status == "pending"
        _assert_unresolved(job)

    def test_every_immediate_code_is_one_of_the_three_named_families(self):
        """Completeness, so the set cannot grow a member nobody classified."""
        assert deletion_worker.IMMEDIATE_ATTENTION_CODES == (
            deletion_worker.CONFIGURATION_ERROR_CODES
            | deletion_worker.PROTECTED_ERROR_CODES
            | deletion_worker.AUTHORIZATION_ERROR_CODES
        )
        assert erasure.ERASURE_VERSIONS_REMAIN not in deletion_worker.IMMEDIATE_ATTENTION_CODES


class TestAProviderThatCannotErase:
    def test_the_job_is_held_rather_than_the_pass_crashing(self, db_session):
        job = _one_job(db_session)

        report = deletion_worker.run_once(db_session, CannotErase())
        db_session.refresh(job)

        assert job.status == "needs_attention"
        assert job.last_error_class == erasure.ERASURE_NOT_IMPLEMENTED
        assert report.needs_attention == 1
        _assert_unresolved(job)

    def test_it_does_not_fall_back_to_a_plain_delete(self, db_session):
        """The fallback would be the original defect: on a versioned bucket it inserts a
        marker, reports success and leaves every byte in place. The double asserts it."""
        _one_job(db_session)

        deletion_worker.run_once(db_session, CannotErase())

    def test_the_default_on_the_interface_refuses_the_same_way(self, db_session):
        """An adapter that simply has not implemented erasure reaches the base class default,
        which raises with the same code -- so both routes end in a held job, not a delete."""
        job = _one_job(db_session)

        class Inheriting(StorageProvider):
            name = "local"

            def identity(self):
                return StorageIdentity(provider="local")

            def save(self, key, data):  # pragma: no cover - not exercised
                return key

            def delete(self, key):  # pragma: no cover - must never be reached
                raise AssertionError("no fallback to delete()")

            def load(self, key):  # pragma: no cover - not exercised
                raise NotImplementedError

        deletion_worker.run_once(db_session, Inheriting())
        db_session.refresh(job)

        assert job.status == "needs_attention"
        assert job.last_error_class == f"StorageError:{erasure.ERASURE_NOT_IMPLEMENTED}"
        _assert_unresolved(job)


class TestNothingPersonalReachesTheRecord:
    def test_the_recorded_reason_carries_no_key_bucket_or_message(self, db_session):
        """`last_error_class` is read by operators and may be shipped to logs. A provider
        message routinely quotes the bucket and the object key, which is why only the class and
        the service code are kept."""
        job = _one_job(db_session)
        storage = Erasing(
            error=StorageError(
                f"Failed to delete '{_KEY}' from bucket talviora-secret-bucket",
                code="AccessDenied",
            )
        )

        deletion_worker.run_once(db_session, storage)
        db_session.refresh(job)

        assert job.last_error_class == "StorageError:AccessDenied"
        assert _KEY not in job.last_error_class
        assert "talviora-secret-bucket" not in job.last_error_class
