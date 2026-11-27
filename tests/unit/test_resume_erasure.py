"""Deleting one resume has to erase its file, not just stop listing it.

`delete_resume` used to call `storage.delete`, catch every exception, log a warning and delete
the database rows anyway. Two defects in one line. On a versioned bucket a delete without a
version id inserts a delete marker and erases nothing, so the student was told their resume was
permanently deleted while every byte stayed stored. And on any failure the rows went regardless,
which destroyed the only remaining pointer to an object that was still there -- the exact
invariant the account-deletion outbox exists to hold.

So the assertions here are about three things:

* `erase` is what gets called, and `delete` never is -- a fallback to `delete` would silently
  restore the first defect, which is why that is asserted directly rather than inferred;
* a refused erasure leaves the resume row, its derived rows and the file reference untouched,
  because a student whose deletion failed still owns their resume;
* repeating a deletion is free, which is what makes erasing before committing safe.

The S3 half is mocked at the boto3 client boundary, the same way `tests/unit/test_s3_erasure.py`
is, and proves the request shapes rather than AWS's behaviour. Nothing here touches a real
bucket, and the database is a throwaway SQLite file with foreign keys enforced.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.database.base import Base
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.user import User
from app.services import resume_service
from app.services.resume_service import ResumeErasureFailed, delete_resume
from app.storage import erasure, identity
from app.storage.identity import StorageIdentity
from app.storage.provider import StorageError
from app.storage.s3_provider import S3StorageProvider
from tests.storage_doubles import ErasesWithoutHistory

KEY = "resumes/7/cv.pdf"


@pytest.fixture()
def db(tmp_path):
    """A throwaway database with foreign keys actually enforced.

    SQLite ignores them unless asked, per connection, and production is PostgreSQL, which
    enforces them on every statement. Without the pragma this module could not see the class
    of bug that matters most here: a row deleted while something still points at it.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'erasure.db'}")

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


def make_resume(
    db, profile, *, key: str = KEY, with_analysis: bool = True, stored_by=None
) -> Resume:
    """A resume with the derived row the FK clean-up would delete.

    The analysis matters to several tests below: it is destroyed by `delete_resume`'s FK
    clean-up, so it is the evidence that the clean-up did not run when an erasure was refused.

    `stored_by` is the provider that would have written the file, and its identity is recorded
    on the row the way a real upload records it. It is not decoration: every object path on a
    resume now refuses unless the recorded destination matches the live provider, so a resume
    created without one is a resume no storage can act on. The default matches `_Identified`,
    which is what most doubles below report; tests driving the real S3 adapter pass the
    provider itself, because its identity carries a bucket and an endpoint that a bare name
    cannot.
    """
    destination = (
        stored_by.identity() if stored_by is not None else StorageIdentity(provider="double")
    )
    resume = Resume(
        profile_id=profile.id,
        file_path=key,
        original_filename="cv.pdf",
        raw_text="Python SQL",
        storage_provider=destination.provider,
        storage_bucket=destination.bucket,
        storage_endpoint=destination.endpoint,
        storage_region=destination.region,
        storage_expected_owner=destination.expected_owner,
        storage_config_revision=destination.revision,
    )
    db.add(resume)
    db.commit()
    if with_analysis:
        db.add(
            ResumeAnalysis(
                resume_id=resume.id,
                extracted_skills=["Python"],
                extracted_education=[],
                extracted_experience=[],
                extracted_projects=[],
                extracted_certifications=[],
                ats_score=70.0,
                weak_sections=[],
                extraction_confidence=0.8,
            )
        )
        db.commit()
    return resume


# --- storage doubles -----------------------------------------------------------------------
#
# Each one states exactly how it fails, so that a test asserting on a refusal is asserting on
# that failure and not on an accident of the double. None of them subclasses StorageProvider:
# the service reaches them through `getattr`, which is what a non-conforming object would do.


class _Identified:
    """Can say where it points, which every destructive path requires."""

    def identity(self):
        return StorageIdentity(provider="double")


class RecordingStorage(_Identified, ErasesWithoutHistory):
    """Erases successfully and remembers which methods were reached."""

    def __init__(self):
        self.erased = []
        self.deleted = []

    def erase(self, key):
        self.erased.append(key)
        return erasure.ErasureOutcome(
            versioning=erasure.VERSIONING_NONE,
            verified_by=erasure.VERIFIED_BY_ABSENCE_WITHOUT_HISTORY,
            versions_deleted=1,
        )

    def delete(self, key):
        self.deleted.append(key)


class FailingStorage(_Identified):
    """Raises a chosen erasure code, and records whether `delete` was tried afterwards."""

    def __init__(self, code):
        self.code = code
        self.deleted = []

    def erase(self, key):
        raise StorageError(f"refused: {self.code}", code=self.code)

    def delete(self, key):
        self.deleted.append(key)


class NoErasureStorage(_Identified):
    """Predates the erasure contract: it can delete, and that is all."""

    def __init__(self):
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)


class AnonymousStorage(ErasesWithoutHistory):
    """Erases, but cannot state its destination -- so it must not be allowed to."""

    def __init__(self):
        self.erased = []

    def identity(self):
        return None

    def erase(self, key):
        self.erased.append(key)
        return erasure.ErasureOutcome(
            versioning=erasure.VERSIONING_NONE,
            verified_by=erasure.VERIFIED_BY_ABSENCE_WITHOUT_HISTORY,
        )

    def delete(self, key):
        raise AssertionError("delete must never be reached")


@pytest.fixture()
def s3(monkeypatch):
    monkeypatch.setattr(settings, "storage_s3_bucket", "test-bucket")
    monkeypatch.setattr(settings, "storage_s3_endpoint_url", "https://s3.example.com")
    monkeypatch.setattr(settings, "storage_s3_expected_bucket_owner", None)
    provider = S3StorageProvider()
    provider._client = MagicMock()
    return provider


def _history(key, versions=(), markers=()):
    return {
        "Versions": [{"Key": key, "VersionId": v} for v in versions],
        "DeleteMarkers": [{"Key": key, "VersionId": v} for v in markers],
        "IsTruncated": False,
    }


def _versioned_then_empty(s3, key, versions=(), markers=()):
    """A versioned bucket holding this history, emptied by the delete."""
    s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    s3._client.list_object_versions.side_effect = [
        _history(key, versions, markers),
        _history(key),
    ]
    s3._client.delete_objects.side_effect = lambda **kw: {
        "Deleted": kw["Delete"]["Objects"],
        "Errors": [],
    }


class TestDeletingAResumeOnVersionedStorage:
    def test_every_stored_version_is_deleted_by_id(self, db, s3):
        """The defect, closed. A delete without a version id is what left the data behind."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        _versioned_then_empty(s3, KEY, versions=("v1", "v2", "v3"))

        assert delete_resume(db, resume.id, profile.id, s3) is True

        asked = s3._client.delete_objects.call_args.kwargs["Delete"]["Objects"]
        assert [o["VersionId"] for o in asked] == ["v1", "v2", "v3"]
        assert all(o["Key"] == KEY for o in asked)
        assert db.query(Resume).count() == 0

    def test_a_delete_marker_is_removed_too(self, db, s3):
        """A marker left behind keeps the key listed as deleted with live versions under it."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        _versioned_then_empty(s3, KEY, versions=("v1",), markers=("m1",))

        assert delete_resume(db, resume.id, profile.id, s3) is True

        asked = s3._client.delete_objects.call_args.kwargs["Delete"]["Objects"]
        assert {o["VersionId"] for o in asked} == {"v1", "m1"}

    def test_no_version_less_delete_is_ever_issued(self, db, s3):
        """The whole point. `delete_object` without a VersionId is the original bug."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        _versioned_then_empty(s3, KEY, versions=("v1",))

        delete_resume(db, resume.id, profile.id, s3)

        s3._client.delete_object.assert_not_called()

    def test_governance_bypass_is_never_sent(self, db, s3):
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        _versioned_then_empty(s3, KEY, versions=("v1",))

        delete_resume(db, resume.id, profile.id, s3)

        for call in s3._client.delete_objects.call_args_list:
            assert "BypassGovernanceRetention" not in call.kwargs

    def test_a_remaining_version_is_not_reported_as_a_deletion(self, db, s3):
        """Verification, not optimism: the second enumeration still finds v2."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.side_effect = [
            _history(KEY, ("v1", "v2")),
            _history(KEY, ("v2",)),
        ]
        s3._client.delete_objects.side_effect = lambda **kw: {
            "Deleted": kw["Delete"]["Objects"],
            "Errors": [],
        }

        with pytest.raises(ResumeErasureFailed) as caught:
            delete_resume(db, resume.id, profile.id, s3)

        assert caught.value.code == erasure.ERASURE_VERSIONS_REMAIN
        assert db.query(Resume).count() == 1


class TestTheResumeSurvivesARefusedErasure:
    """The invariant: never destroy the last pointer to an object that is still stored."""

    @pytest.mark.parametrize(
        "code",
        [
            erasure.ERASURE_OBJECT_LOCKED,
            erasure.ERASURE_LEGAL_HOLD,
            erasure.ERASURE_VERSIONS_REMAIN,
            erasure.ERASURE_VERSIONING_UNKNOWN,
            erasure.ERASURE_INVENTORY_UNSUPPORTED,
            erasure.ERASURE_INVENTORY_BROKEN,
            "NoSuchBucket",
            "AccessDenied",
            "SlowDown",
        ],
    )
    def test_nothing_is_deleted_whatever_the_reason(self, db, code):
        profile = make_profile(db)
        resume = make_resume(db, profile)
        storage = FailingStorage(code)

        with pytest.raises(ResumeErasureFailed) as caught:
            delete_resume(db, resume.id, profile.id, storage)

        assert caught.value.code == code
        db.rollback()
        assert db.query(Resume).count() == 1
        assert db.get(Resume, resume.id).file_path == KEY, "the key must survive to be retried"
        assert db.query(ResumeAnalysis).count() == 1, "derived rows must survive too"

    def test_delete_is_never_tried_as_a_fallback(self, db):
        """A fallback would reinstate the original defect precisely when it matters most:
        a bucket that refused the proper erasure would get a delete marker instead."""
        profile = make_profile(db)
        resume = make_resume(db, profile)
        storage = FailingStorage(erasure.ERASURE_OBJECT_LOCKED)

        with pytest.raises(ResumeErasureFailed):
            delete_resume(db, resume.id, profile.id, storage)

        assert storage.deleted == []

    def test_the_failure_names_a_condition_and_not_a_destination(self, db):
        """The message reaches a log and, in a 500, nearly reached a student. A bucket name,
        an endpoint or a key in it would be a configuration leak."""
        profile = make_profile(db)
        resume = make_resume(db, profile)

        with pytest.raises(ResumeErasureFailed) as caught:
            delete_resume(db, resume.id, profile.id, FailingStorage("AccessDenied"))

        message = str(caught.value)
        assert "test-bucket" not in message
        assert KEY not in message
        assert "s3.example.com" not in message

    def test_a_transient_failure_is_distinguishable_from_a_permanent_one(self, db):
        """Classified from the shared sets, so this path cannot be more permissive than the
        worker's."""
        assert "SlowDown" not in erasure.IMMEDIATE_ATTENTION_CODES
        assert erasure.ERASURE_VERSIONS_REMAIN not in erasure.IMMEDIATE_ATTENTION_CODES
        assert erasure.ERASURE_OBJECT_LOCKED in erasure.PROTECTED_ERROR_CODES
        assert "AccessDenied" in erasure.AUTHORIZATION_ERROR_CODES

    def test_a_transport_fault_from_a_real_provider_refuses_rather_than_escapes(self, db, s3):
        """An EndpointConnectionError must arrive as a refusal, not as a raw botocore error
        escaping through the service into a 500 with a stack trace."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        s3._client.get_bucket_versioning.side_effect = EndpointConnectionError(
            endpoint_url="https://s3.example.com"
        )

        with pytest.raises(ResumeErasureFailed):
            delete_resume(db, resume.id, profile.id, s3)

        db.rollback()
        assert db.query(Resume).count() == 1


class TestAProviderThatCannotProveAnErasure:
    def test_one_with_no_erase_at_all_is_refused(self, db):
        """Real adapters cannot reach this -- the base class supplies a refusing `erase` -- but
        a duck-typed object can, and it must not reach `delete` instead."""
        profile = make_profile(db)
        resume = make_resume(db, profile)
        storage = NoErasureStorage()

        with pytest.raises(ResumeErasureFailed) as caught:
            delete_resume(db, resume.id, profile.id, storage)

        assert caught.value.code == erasure.ERASURE_NOT_IMPLEMENTED
        assert storage.deleted == []
        db.rollback()
        assert db.query(Resume).count() == 1

    def test_the_base_provider_refuses_by_default(self, db):
        """The default is concrete and refuses, so an adapter that forgets version-aware
        erasure fails closed rather than inheriting a permissive no-op."""
        from app.storage.provider import StorageProvider

        class Forgetful(StorageProvider):
            name = "forgetful"

            def save(self, key, data):
                raise NotImplementedError

            def load(self, key):
                raise NotImplementedError

            def delete(self, key):
                raise AssertionError("delete must never be reached")

        profile = make_profile(db)
        # Stored *by this provider*, so the refusal under test is the missing `erase` and not
        # a destination mismatch standing in for it.
        storage = Forgetful()
        resume = make_resume(db, profile, stored_by=storage)

        with pytest.raises(ResumeErasureFailed) as caught:
            delete_resume(db, resume.id, profile.id, storage)

        assert caught.value.code == erasure.ERASURE_NOT_IMPLEMENTED

    def test_one_that_cannot_state_its_destination_never_erases(self, db):
        """`identity()` returning None means the instance cannot say where it points. The base
        class is explicit that such an instance must not be treated as pointing at the right
        place, so the destructive call is not made at all.

        The resume *does* have a recorded destination here, so this is not the legacy case: it
        is a live provider that cannot be compared against anything, which is why it reports
        `IDENTITY_UNDETERMINABLE` rather than `IDENTITY_UNKNOWN`. The two are kept apart
        because the fixes differ -- one needs a provider that answers, the other needs an
        operator to recover a destination that was never written down.
        """
        profile = make_profile(db)
        resume = make_resume(db, profile)
        storage = AnonymousStorage()

        with pytest.raises(resume_service.ResumeStorageUnconfirmed) as caught:
            delete_resume(db, resume.id, profile.id, storage)

        assert caught.value.code == identity.IDENTITY_UNDETERMINABLE
        assert storage.erased == [], "the refusal must come before the erasure, not after"
        db.rollback()
        assert db.query(Resume).count() == 1

    def test_a_resume_with_no_recorded_destination_is_refused(self, db):
        """The legacy row: uploaded before identity capture existed, so nothing says which
        bucket holds its file. Refused rather than attempted against current configuration,
        because a key that is absent from the wrong bucket is an idempotent success -- the rows
        would go and the object would stay. There is no provider that can satisfy this; it
        needs a human, which is what `scripts/review_resume_identity.py` is for.
        """
        profile = make_profile(db)
        resume = make_resume(db, profile)
        for column in (
            "storage_provider",
            "storage_bucket",
            "storage_endpoint",
            "storage_region",
            "storage_expected_owner",
            "storage_config_revision",
        ):
            setattr(resume, column, None)
        db.commit()
        storage = RecordingStorage()

        with pytest.raises(resume_service.ResumeStorageUnconfirmed) as caught:
            delete_resume(db, resume.id, profile.id, storage)

        assert caught.value.code == identity.IDENTITY_UNKNOWN
        assert storage.erased == [], "a legacy resume must not be erased from a guessed bucket"
        assert storage.deleted == [], "and certainly not through the old delete path"
        db.rollback()
        assert db.query(Resume).count() == 1

    def test_the_identity_refusal_uses_one_vocabulary(self):
        """Same refusal, same word. An operator reading the worker's log or the API's log
        should not have to learn two names for one condition, which is why both read their
        codes from `app.storage.identity` rather than each defining its own."""
        from app.workers import deletion_worker

        assert deletion_worker.IDENTITY_UNKNOWN is identity.IDENTITY_UNKNOWN
        assert deletion_worker.IDENTITY_FAILURES is identity.IDENTITY_FAILURES


class TestRepeatingADeletion:
    def test_an_already_absent_object_is_an_idempotent_success(self, db, s3):
        """What makes erase-then-commit safe: a crash between the two leaves a row pointing at
        an object that is already gone, and the retry has to succeed."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.return_value = _history(KEY)

        assert delete_resume(db, resume.id, profile.id, s3) is True

        s3._client.delete_objects.assert_not_called()
        assert db.query(Resume).count() == 0

    def test_a_retry_adds_no_delete_marker(self, db, s3):
        """A version-less delete adds a marker every time it is repeated. This must not."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.return_value = _history(KEY)

        delete_resume(db, resume.id, profile.id, s3)

        s3._client.delete_object.assert_not_called()
        s3._client.delete_objects.assert_not_called()

    def test_deleting_the_same_resume_twice_reports_absent_not_erased(self, db):
        """Second call finds no row, so it never reaches storage at all."""
        profile = make_profile(db)
        resume = make_resume(db, profile)
        storage = RecordingStorage()

        assert delete_resume(db, resume.id, profile.id, storage) is True
        assert delete_resume(db, resume.id, profile.id, storage) is False
        assert storage.erased == [KEY], "the second call must not erase anything again"


class TestOwnership:
    def test_another_profiles_resume_is_neither_erased_nor_deleted(self, db):
        """Ownership is checked before anything destructive. A cross-user deletion that erased
        the file first and only then discovered it was not allowed would be unrecoverable."""
        mine = make_profile(db)
        theirs = make_profile(db)
        resume = make_resume(db, theirs)
        storage = RecordingStorage()

        assert delete_resume(db, resume.id, mine.id, storage) is False

        assert storage.erased == []
        assert storage.deleted == []
        assert db.query(Resume).count() == 1

    def test_a_missing_resume_reaches_no_storage(self, db):
        profile = make_profile(db)
        storage = RecordingStorage()

        assert delete_resume(db, 999_999, profile.id, storage) is False

        assert storage.erased == []


class TestTheRowsOutliveTheObject:
    def test_the_erasure_happens_before_any_row_is_touched(self, db):
        """Order, asserted directly. The FK clean-up deletes this resume's analyses, so running
        it first and then failing would throw away derived data for a resume that survives."""
        profile = make_profile(db)
        resume = make_resume(db, profile)
        seen = {}

        class ObservingStorage(_Identified, ErasesWithoutHistory):
            def erase(self, key):
                seen["analyses"] = db.query(ResumeAnalysis).count()
                seen["resumes"] = db.query(Resume).count()
                return erasure.ErasureOutcome(
                    versioning=erasure.VERSIONING_NONE,
                    verified_by=erasure.VERIFIED_BY_ABSENCE_WITHOUT_HISTORY,
                )

            def delete(self, key):
                raise AssertionError("delete must never be reached")

        assert delete_resume(db, resume.id, profile.id, ObservingStorage()) is True

        assert seen == {"analyses": 1, "resumes": 1}, (
            "both rows must still exist at the moment the object is erased"
        )
        assert db.query(Resume).count() == 0
        assert db.query(ResumeAnalysis).count() == 0

    def test_a_verified_erasure_is_what_releases_the_rows(self, db):
        profile = make_profile(db)
        resume = make_resume(db, profile)

        assert delete_resume(db, resume.id, profile.id, RecordingStorage()) is True

        assert db.query(Resume).count() == 0
        assert db.query(ResumeAnalysis).count() == 0


class TestNoFallbackToDeleteAnywhere:
    def test_the_service_never_calls_delete_on_a_successful_path(self, db):
        profile = make_profile(db)
        resume = make_resume(db, profile)
        storage = RecordingStorage()

        delete_resume(db, resume.id, profile.id, storage)

        assert storage.erased == [KEY]
        assert storage.deleted == [], "delete() must not be in the resume deletion path at all"

    def test_the_source_contains_no_storage_delete_call(self):
        """A grep, deliberately. Every other test here can be satisfied by a double; this one
        notices if a `storage.delete(...)` is reintroduced anywhere in the module, including on
        a branch no test happens to cover."""
        import inspect

        source = inspect.getsource(resume_service)
        assert "storage.delete(" not in source
        assert ".delete(resume.file_path)" not in source


class TestWhatTheProviderIsAskedAboutTheBucket:
    def test_an_unversioned_bucket_is_erased_through_the_defined_path(self, db, s3):
        """No history can exist, so absence is sufficient evidence -- and the outcome says so
        rather than letting it look like an enumeration."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        s3._client.get_bucket_versioning.return_value = {}
        s3._client.head_object.side_effect = [
            {"ContentLength": 1},
            ClientError({"Error": {"Code": "404", "Message": "no"}}, "HeadObject"),
        ]

        assert delete_resume(db, resume.id, profile.id, s3) is True

        s3._client.delete_object.assert_called_once()
        assert db.query(Resume).count() == 0

    def test_an_unrecognised_versioning_mode_refuses(self, db, s3):
        """Unknown is never zero. Guessing "unversioned" here would accept a 404 as proof on a
        bucket that may hold history."""
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        s3._client.get_bucket_versioning.return_value = {"Status": "Sideways"}

        with pytest.raises(ResumeErasureFailed) as caught:
            delete_resume(db, resume.id, profile.id, s3)

        assert caught.value.code == erasure.ERASURE_VERSIONING_UNKNOWN
        db.rollback()
        assert db.query(Resume).count() == 1

    def test_a_missing_bucket_is_never_a_successful_erasure(self, db, s3):
        profile = make_profile(db)
        resume = make_resume(db, profile, stored_by=s3)
        s3._client.get_bucket_versioning.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "no"}}, "GetBucketVersioning"
        )

        with pytest.raises(ResumeErasureFailed) as caught:
            delete_resume(db, resume.id, profile.id, s3)

        assert caught.value.code == "NoSuchBucket"
        db.rollback()
        assert db.query(Resume).count() == 1
