"""A deletion job may only be erased against the storage it was enqueued against.

The invariant these tests exist to defend is narrower and sharper than "deletion works": an
unconfirmed destination must produce **no storage call at all** and **no `done` status**. Both
halves matter. A refused job that still called `delete` would have erased something somewhere;
a refused job that still reached `done` would close its request, and closing a request deletes
the last row holding the object's key -- so the object becomes unreachable and nothing records
that it was never erased.

That is why most assertions here are about what did *not* happen. `storage.deleted == []` is
the load-bearing one, and `TestNothingUnverifiedIsEverErased` asserts it across every way a
destination can fail to be confirmed.

SQLite, with the same in-memory engine the sibling outbox tests use. The identity rule is pure
application logic -- a comparison of recorded columns against a provider's own report -- so the
engine is not the interesting variable here; the concurrency behaviour that *is* engine-specific
stays in tests/unit/test_deletion_outbox_postgres.py.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.deletion_job import DeletionJob
from app.models.deletion_request import DeletionRequest
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.user import User
from app.services.account_service import delete_account
from app.storage.identity import StorageIdentity
from app.storage.provider import StorageError, StorageProvider
from app.workers import deletion_worker
from tests.storage_doubles import ErasesWithoutHistory

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


def _seed_account(db, keys=(_KEY,)):
    user = User(email="leaving@example.com", password_hash="x", is_active=True, token_version=3)
    db.add(user)
    db.flush()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.flush()
    for index, key in enumerate(keys):
        db.add(
            Resume(
                profile_id=profile.id, file_path=key, raw_text=f"r{index}", parse_status="done"
            )
        )
    db.commit()
    return user, profile


class Recording(ErasesWithoutHistory, StorageProvider):
    """Records what it was asked to delete, and can be pointed anywhere.

    `name` and `identity()` are both explicit because the worker verifies a destination before
    erasing: a double that cannot say where it points is refused, which is the behaviour under
    test rather than an inconvenience.
    """

    name = "local"

    def __init__(self, identity=None, error=None):
        self.deleted: list[str] = []
        self._identity = identity if identity is not None else StorageIdentity(provider="local")
        # Kept consistent with the identity on purpose. The worker checks the provider name
        # first and the full destination second, so a double whose name disagreed with its own
        # identity would trip the first check and never exercise the second.
        self.name = self._identity.provider
        self.error = error

    def identity(self):
        return self._identity

    def save(self, key, data):  # pragma: no cover - not exercised
        return key

    def delete(self, key):
        if self.error is not None:
            raise self.error
        self.deleted.append(key)

    def load(self, key):  # pragma: no cover - not exercised
        raise NotImplementedError


class Mute(Recording):
    """A provider that cannot state where it points, which must never be trusted."""

    def identity(self):
        return None


def _request_one_job(db, storage=None):
    """Phase 1 only: the obligation is recorded, nothing is erased yet."""
    user, _ = _seed_account(db)
    delete_account(db, user, storage or Recording())
    return db.query(DeletionJob).one()


def _as_legacy(db, job):
    """Make a job look like one created before identity capture existed."""
    job.storage_bucket = None
    job.storage_endpoint = None
    job.storage_region = None
    job.storage_config_revision = None
    db.commit()
    return job


class TestIdentityIsCapturedWhenTheJobIsCreated:
    def test_an_s3_job_records_its_destination(self, db_session, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "storage_provider", "s3")
        monkeypatch.setattr(settings, "storage_s3_bucket", "talviora-resumes")
        monkeypatch.setattr(settings, "storage_s3_endpoint_url", "https://e.example.com")
        monkeypatch.setattr(settings, "storage_s3_region", "ap-south-1")

        job = _request_one_job(db_session)

        assert job.storage_provider == "s3"
        assert job.storage_bucket == "talviora-resumes"
        assert job.storage_endpoint == "https://e.example.com"
        assert job.storage_region == "ap-south-1"
        # Present, and the marker that capture happened at all.
        assert job.storage_config_revision

    def test_a_local_job_records_a_revision_even_with_no_bucket(self, db_session):
        """`local` has no bucket, so without the revision there would be no way to tell a job
        created before identity capture from one created after it."""
        job = _request_one_job(db_session)

        assert job.storage_provider == "local"
        assert job.storage_bucket is None
        assert job.storage_config_revision

    def test_no_credential_is_written_to_the_job(self, db_session, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "storage_provider", "s3")
        monkeypatch.setattr(settings, "storage_s3_bucket", "talviora-resumes")
        monkeypatch.setattr(settings, "storage_s3_access_key_id", "AKIAEXAMPLE")
        monkeypatch.setattr(settings, "storage_s3_secret_access_key", "s3cr3t-value")

        job = _request_one_job(db_session)

        written = " ".join(
            str(v)
            for v in (
                job.storage_provider,
                job.storage_bucket,
                job.storage_endpoint,
                job.storage_region,
                job.storage_config_revision,
            )
        )
        assert "AKIAEXAMPLE" not in written
        assert "s3cr3t-value" not in written


class TestNothingUnverifiedIsEverErased:
    """The demonstration: no way of failing to confirm a destination can reach storage.

    Each case ends with the same three assertions -- nothing was deleted, the job is not done,
    and the request is still open -- because those three together are the invariant. A job that
    reached `done` would close its request, and that deletes the last row holding the key.
    """

    def _run(self, db, job, storage):
        report = deletion_worker.run_once(db, storage)
        db.refresh(job)
        return report

    def _assert_refused(self, db, job, storage, expected_reason):
        assert storage.deleted == [], "an unverified destination must not reach storage"
        assert job.status != "done", "an unverified job must never be marked done"
        assert job.last_error_class == expected_reason
        assert db.query(DeletionRequest).one().status != "done", (
            "the request must stay open; closing it deletes the last pointer to the object"
        )

    def test_a_job_that_predates_identity_capture_is_held(self, db_session):
        job = _as_legacy(db_session, _request_one_job(db_session))
        storage = Recording()

        self._run(db_session, job, storage)

        self._assert_refused(db_session, job, storage, deletion_worker.IDENTITY_UNKNOWN)

    def test_an_identity_that_cannot_target_anything_is_held(self, db_session):
        """Recorded, but unusable: `s3` with no bucket names no destination."""
        job = _request_one_job(db_session)
        job.storage_provider = "s3"
        job.storage_bucket = None
        job.storage_config_revision = "deadbeefdeadbeef"
        db_session.commit()
        storage = Recording(identity=StorageIdentity(provider="s3", bucket="anything"))

        self._run(db_session, job, storage)

        self._assert_refused(db_session, job, storage, deletion_worker.IDENTITY_INCOMPLETE)

    def test_a_provider_that_will_not_say_where_it_points_is_held(self, db_session):
        job = _request_one_job(db_session)
        storage = Mute()

        self._run(db_session, job, storage)

        self._assert_refused(db_session, job, storage, deletion_worker.IDENTITY_UNDETERMINABLE)

    def test_a_bucket_changed_after_enqueue_is_held(self, db_session):
        """The defect this whole stage exists for.

        The job was enqueued against one bucket and the worker is pointed at another. Deleting
        the key from the wrong bucket would *succeed* -- it is absent there, and absence is
        idempotent success -- so the job would be marked done and the request closed, erasing
        the last record of an object that is still sitting in the original bucket.
        """
        job = _request_one_job(db_session)
        job.storage_provider = "s3"
        job.storage_bucket = "talviora-resumes"
        job.storage_config_revision = "0123456789abcdef"
        db_session.commit()
        storage = Recording(
            identity=StorageIdentity(provider="s3", bucket="talviora-resumes-OLD")
        )

        self._run(db_session, job, storage)

        self._assert_refused(db_session, job, storage, deletion_worker.IDENTITY_MISMATCH)

    def test_a_region_change_alone_is_enough_to_hold_it(self, db_session):
        """A same-named bucket in another region is a different bucket."""
        job = _request_one_job(db_session)
        job.storage_provider = "s3"
        job.storage_bucket = "talviora-resumes"
        job.storage_region = "ap-south-1"
        job.storage_config_revision = "0123456789abcdef"
        db_session.commit()
        storage = Recording(
            identity=StorageIdentity(
                provider="s3", bucket="talviora-resumes", region="us-east-1"
            )
        )

        self._run(db_session, job, storage)

        self._assert_refused(db_session, job, storage, deletion_worker.IDENTITY_MISMATCH)

    def test_every_declared_failure_mode_is_reachable(self):
        """Completeness, so a mode cannot be declared and then never actually detected.

        `identity_refusal` reads attributes off the job, so a stand-in is enough and keeps each
        scenario to the one field that makes it fail. If a fifth failure mode is added to
        IDENTITY_FAILURES without a scenario here, this fails rather than leaving dead code.
        """
        local = StorageIdentity(provider="local")
        s3 = StorageIdentity(provider="s3", bucket="b")
        scenarios = {
            deletion_worker.IDENTITY_UNKNOWN: (
                SimpleNamespace(
                    storage_provider="local",
                    storage_bucket=None,
                    storage_endpoint=None,
                    storage_region=None,
                    storage_expected_owner=None,
                    storage_config_revision=None,
                ),
                Recording(identity=local),
            ),
            deletion_worker.IDENTITY_INCOMPLETE: (
                SimpleNamespace(
                    storage_provider="s3",
                    storage_bucket=None,
                    storage_endpoint=None,
                    storage_region=None,
                    storage_expected_owner=None,
                    storage_config_revision="r",
                ),
                Recording(identity=s3),
            ),
            deletion_worker.IDENTITY_UNDETERMINABLE: (
                SimpleNamespace(
                    storage_provider="local",
                    storage_bucket=None,
                    storage_endpoint=None,
                    storage_region=None,
                    storage_expected_owner=None,
                    storage_config_revision="r",
                ),
                Mute(),
            ),
            deletion_worker.IDENTITY_MISMATCH: (
                SimpleNamespace(
                    storage_provider="s3",
                    storage_bucket="b",
                    storage_endpoint=None,
                    storage_region=None,
                    storage_expected_owner=None,
                    storage_config_revision="r",
                ),
                Recording(identity=StorageIdentity(provider="s3", bucket="elsewhere")),
            ),
            deletion_worker.IDENTITY_OWNER_MISMATCH: (
                SimpleNamespace(
                    storage_provider="s3",
                    storage_bucket="b",
                    storage_endpoint=None,
                    storage_region=None,
                    storage_expected_owner="111111111111",
                    storage_config_revision="r",
                ),
                Recording(
                    identity=StorageIdentity(
                        provider="s3", bucket="b", expected_owner="222222222222"
                    )
                ),
            ),
            deletion_worker.IDENTITY_OWNER_UNCONFIRMED: (
                SimpleNamespace(
                    storage_provider="s3",
                    storage_bucket="b",
                    storage_endpoint=None,
                    storage_region=None,
                    storage_expected_owner="111111111111",
                    storage_config_revision="r",
                ),
                # Same destination, but this worker has no owner guard to assert.
                Recording(identity=StorageIdentity(provider="s3", bucket="b")),
            ),
        }

        assert set(scenarios) == deletion_worker.IDENTITY_FAILURES, (
            "a failure mode is declared but has no scenario proving it can be detected"
        )
        for expected, (job, storage) in scenarios.items():
            assert deletion_worker.identity_refusal(job, storage) == expected

    def test_a_confirmed_destination_produces_no_refusal(self):
        """The same helper must say yes when the destination does match."""
        job = SimpleNamespace(
            storage_provider="s3",
            storage_bucket="b",
            storage_endpoint="https://e",
            storage_region="r1",
            storage_expected_owner=None,
            storage_config_revision="rev",
        )
        storage = Recording(
            identity=StorageIdentity(
                provider="s3", bucket="b", endpoint="https://e", region="r1"
            )
        )

        assert deletion_worker.identity_refusal(job, storage) is None


class TestARefusalLeavesTheJobRecoverable:
    def test_the_lease_is_released_so_another_worker_can_retry(self, db_session):
        job = _as_legacy(db_session, _request_one_job(db_session))

        deletion_worker.run_once(db_session, Recording())
        db_session.refresh(job)

        assert job.locked_until is None, "a held job must not keep a lease it is not working"
        assert job.status in deletion_worker.UNRESOLVED_STATUSES
        assert job.next_attempt_at is not None, "it must come back, with backoff"

    def test_it_resumes_by_itself_once_the_identity_matches(self, db_session):
        """No re-queueing step: the refusal keeps the job claimable for exactly this reason."""
        job = _request_one_job(db_session)
        job.storage_provider = "s3"
        job.storage_bucket = "right-bucket"
        job.storage_config_revision = "0123456789abcdef"
        db_session.commit()

        wrong = Recording(identity=StorageIdentity(provider="s3", bucket="wrong-bucket"))
        deletion_worker.run_once(db_session, wrong)
        db_session.refresh(job)
        assert wrong.deleted == []
        assert job.status == "needs_attention"

        # Make the job due again, then point a worker at the storage it was enqueued against.
        job.next_attempt_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            minutes=1
        )
        db_session.commit()
        right = Recording(identity=StorageIdentity(provider="s3", bucket="right-bucket"))

        deletion_worker.run_once(db_session, right)
        db_session.refresh(job)

        assert right.deleted == [_KEY]
        assert job.status == "done"
        assert job.last_error_class is None

    def test_a_confirmed_destination_still_erases(self, db_session):
        """The control. A guard that refused everything would pass every test above."""
        job = _request_one_job(db_session)
        storage = Recording()

        report = deletion_worker.run_once(db_session, storage)
        db_session.refresh(job)

        assert storage.deleted == [_KEY]
        assert job.status == "done"
        assert report.requests_closed == 1


class TestAMissingBucketIsNotAnErasure:
    def test_the_s3_adapter_raises_instead_of_reporting_success(self):
        """It used to be in `_ALREADY_GONE`, which made a missing bucket a completed erasure."""
        from app.storage.s3_provider import S3StorageProvider

        provider = S3StorageProvider.__new__(S3StorageProvider)
        provider.bucket = "talviora-resumes"
        # No owner guard, stated rather than left unset: `_guard()` has no
        # getattr default, so a provider that forgot this would send no
        # ExpectedBucketOwner and nothing would notice.
        provider.expected_owner = None
        provider._client = MagicMock()
        provider._client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "nope"}}, "DeleteObject"
        )

        with pytest.raises(StorageError) as caught:
            provider.delete(_KEY)

        assert caught.value.code == "NoSuchBucket"

    def test_a_missing_key_is_still_an_idempotent_success(self):
        """The distinction being drawn: the object's absence is success, the bucket's is not."""
        from app.storage.s3_provider import S3StorageProvider

        provider = S3StorageProvider.__new__(S3StorageProvider)
        provider.bucket = "talviora-resumes"
        # No owner guard, stated rather than left unset: `_guard()` has no
        # getattr default, so a provider that forgot this would send no
        # ExpectedBucketOwner and nothing would notice.
        provider.expected_owner = None
        provider._client = MagicMock()
        provider._client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "DeleteObject"
        )

        provider.delete(_KEY)  # must not raise

    def test_the_worker_escalates_it_on_the_first_attempt(self, db_session):
        """No number of retries creates a bucket, so it does not wait for the attempt cap."""
        job = _request_one_job(db_session)
        storage = Recording(error=StorageError("boom", code="NoSuchBucket"))

        deletion_worker.run_once(db_session, storage)
        db_session.refresh(job)

        assert job.status == "needs_attention"
        assert job.last_error_class == "StorageError:NoSuchBucket"
        assert job.attempts == 1, "escalated immediately, and attempts not double-counted"
        assert db_session.query(DeletionRequest).one().status != "done"


class TestTheOperatorReview:
    def test_it_names_the_held_jobs_without_naming_a_bucket(self, db_session):
        job = _request_one_job(db_session)
        job.storage_provider = "s3"
        job.storage_bucket = "talviora-secret-bucket"
        job.storage_config_revision = "0123456789abcdef"
        db_session.commit()
        deletion_worker.run_once(
            db_session, Recording(identity=StorageIdentity(provider="s3", bucket="other"))
        )

        review = deletion_worker.review_identities(db_session)

        assert review.unresolved_jobs == 1
        assert review.by_revision == {"0123456789abcdef": 1}
        assert review.held_by_reason == {deletion_worker.IDENTITY_MISMATCH: [job.id]}
        printed = repr(review)
        assert "talviora-secret-bucket" not in printed
        assert _KEY not in printed

    def test_it_separates_jobs_that_never_had_an_identity(self, db_session):
        job = _as_legacy(db_session, _request_one_job(db_session))

        review = deletion_worker.review_identities(db_session)

        assert review.without_identity == [job.id]
        assert review.needs_operator == 1

    def test_it_reads_only(self, db_session):
        """It is run during incidents, so it must not be able to change the queue."""
        job = _as_legacy(db_session, _request_one_job(db_session))
        before = (job.status, job.attempts, job.locked_until, job.next_attempt_at)

        deletion_worker.review_identities(db_session)
        db_session.refresh(job)

        assert (job.status, job.attempts, job.locked_until, job.next_attempt_at) == before
class TestTheBucketOwnerGuard:
    """Bucket, endpoint and region do not pin an account, and the gap is not theoretical.

    An S3 bucket name is globally unique only while the bucket exists: delete it and another
    account may take the name. A multi-tenant S3-compatible endpoint can serve the same bucket
    name to two tenants outright. In both cases all four location fields match, the delete is
    accepted by a bucket nobody here owns, the key is absent, absence reads as idempotent
    success -- and the job is marked done having erased nothing. `ExpectedBucketOwner` closes
    it at the service, which answers 403 instead of taking this process's word for the
    destination.
    """

    def test_the_owner_is_captured_with_the_destination(self, db_session, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "storage_provider", "s3", raising=False)
        monkeypatch.setattr(settings, "storage_s3_bucket", "talviora-prod", raising=False)
        monkeypatch.setattr(
            settings, "storage_s3_expected_bucket_owner", "111111111111", raising=False
        )

        identity = StorageIdentity.from_settings()

        assert identity.expected_owner == "111111111111"

    def test_the_owner_is_not_part_of_the_destination_fingerprint(self):
        """Adding an owner guard must not re-label jobs already in the queue.

        The fingerprint is what an operator groups held jobs by. If it moved when the setting
        was introduced, every queued job would appear to belong to a configuration that no
        longer exists, which is the one thing the report has to be able to say truthfully.
        """
        without = StorageIdentity(provider="s3", bucket="b", region="r1")
        with_owner = StorageIdentity(
            provider="s3", bucket="b", region="r1", expected_owner="111111111111"
        )

        assert with_owner.revision == without.revision
        assert with_owner == without, "equality describes the destination, not the guard"
        assert "111111111111" not in with_owner.describe()

    def test_a_recorded_owner_this_worker_cannot_assert_is_refused(self):
        """The job was created on the promise that ownership would be proved. Dropping the
        proof silently is how a re-created bucket gets a delete it will accept and ignore."""
        job = SimpleNamespace(
            storage_provider="s3",
            storage_bucket="b",
            storage_endpoint=None,
            storage_region=None,
            storage_expected_owner="111111111111",
            storage_config_revision="rev",
        )
        unguarded = Recording(identity=StorageIdentity(provider="s3", bucket="b"))

        assert (
            deletion_worker.identity_refusal(job, unguarded)
            == deletion_worker.IDENTITY_OWNER_UNCONFIRMED
        )

    def test_a_different_owner_is_refused(self):
        job = SimpleNamespace(
            storage_provider="s3",
            storage_bucket="b",
            storage_endpoint=None,
            storage_region=None,
            storage_expected_owner="111111111111",
            storage_config_revision="rev",
        )
        elsewhere = Recording(
            identity=StorageIdentity(provider="s3", bucket="b", expected_owner="222222222222")
        )

        assert (
            deletion_worker.identity_refusal(job, elsewhere)
            == deletion_worker.IDENTITY_OWNER_MISMATCH
        )

    def test_adding_a_guard_does_not_freeze_jobs_that_predate_it(self):
        """The asymmetry, asserted rather than described.

        A job captured before the setting existed has no recorded owner. A worker that now has
        one only narrows what it will accept, and the service enforces it on every call, so
        holding the queue because an operator added a safety setting would punish adding one.
        """
        job = SimpleNamespace(
            storage_provider="s3",
            storage_bucket="b",
            storage_endpoint=None,
            storage_region=None,
            storage_expected_owner=None,
            storage_config_revision="rev",
        )
        guarded = Recording(
            identity=StorageIdentity(provider="s3", bucket="b", expected_owner="111111111111")
        )

        assert deletion_worker.identity_refusal(job, guarded) is None

    def test_an_owner_refusal_erases_nothing_and_does_not_complete(self, db_session):
        job = _request_one_job(db_session)
        job.storage_provider = "s3"
        job.storage_bucket = "b"
        job.storage_expected_owner = "111111111111"
        job.storage_config_revision = "0123456789abcdef"
        db_session.commit()

        storage = Recording(
            identity=StorageIdentity(provider="s3", bucket="b", expected_owner="222222222222")
        )
        deletion_worker.run_once(db_session, storage)
        db_session.refresh(job)

        assert storage.deleted == [], "nothing may be erased against an unproven owner"
        assert job.status == "needs_attention"
        assert job.completed_at is None
        assert job.last_error_class == deletion_worker.IDENTITY_OWNER_MISMATCH

    def test_the_operator_report_never_prints_the_account_id(self, db_session):
        """An account id grants nothing, but it names an account, so it stays out of reports."""
        job = _request_one_job(db_session)
        job.storage_provider = "s3"
        job.storage_bucket = "b"
        job.storage_expected_owner = "999988887777"
        job.storage_config_revision = "0123456789abcdef"
        db_session.commit()

        review = deletion_worker.review_identities(db_session)

        assert "999988887777" not in repr(review)
