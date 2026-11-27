"""A resume's file has to remember which storage it was written to.

`resumes.file_path` is only meaningful to the storage that holds it, and
`get_storage_provider()` builds a provider from current settings on every request. So the key
alone is not enough: a resume uploaded to one bucket and read, scored or deleted after a
reconfiguration is pointed at the wrong place, where the key is simply absent. The read looks
like a lost file. The erasure is worse -- absence is idempotent success, so the rows go and the
object stays, which is the invariant the account-deletion outbox exists to hold.

Stage B.2 records the destination at upload time and refuses every object operation whose
recorded destination is not the live one. These tests cover the capture end of that: what gets
written down, that no credential is among it, that a provider which cannot say where it writes
stores nothing at all, and what survives a database failure after the bytes are already on
disk.

The refusal end -- reads and deletions against a moved destination, and legacy rows with
nothing recorded -- is in tests/unit/test_resume_erasure.py, tests/api/test_resumes.py and
tests/unit/test_deletion_identity.py.

Nothing here touches a real bucket. The local provider writes into a tmp_path, and the S3
adapter is only ever asked what it would report, never to perform I/O.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.ai.provider import AIProvider
from app.ai.schemas.resume import ParsedResume
from app.core.config import settings
from app.database.base import Base
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.user import User
from app.services import resume_service
from app.storage import identity as identity_module
from app.storage.identity import StorageIdentity, recorded_identity
from app.storage.local_provider import LocalStorageProvider
from app.storage.s3_provider import S3StorageProvider
from tests.fixtures.pdf_helpers import make_pdf_with_text

#: The six columns that carry the destination. Named once so a test asserting "no credential is
#: in here" cannot silently miss a column added later.
IDENTITY_COLUMNS = (
    "storage_provider",
    "storage_bucket",
    "storage_endpoint",
    "storage_region",
    "storage_expected_owner",
    "storage_config_revision",
)

PARSED = ParsedResume(
    skills=["Python"],
    education=[],
    experience=[],
    projects=[],
    certifications=[],
    ats_score=70.0,
    weak_sections=[],
    extraction_confidence=0.8,
)


class StubAI(AIProvider):
    """Parses successfully without a model. The AI half is irrelevant here."""

    def generate_structured(self, prompt, schema):
        return PARSED

    def generate_text(self, prompt):  # pragma: no cover - not reached
        return ""


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")

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


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    """The real local provider, writing into a throwaway directory."""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "storage_provider", "local")
    return LocalStorageProvider()


def make_profile(db) -> int:
    """The profile id, not the object.

    `process_resume_upload` crosses an `outside_transaction` boundary that closes the session,
    so an ORM instance held across a call to it is detached and unusable afterwards -- the
    service keeps a plain int for the same reason. Returning the id keeps these tests honest
    about that rather than working around it with a refresh.
    """
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    return profile.id


def upload(db, profile_id, storage, text="Python SQL") -> int:
    created = resume_service.process_resume_upload(
        db,
        profile_id,
        "cv.pdf",
        make_pdf_with_text(text),
        StubAI(),
        storage,
    )
    return created.id


class TestAnUploadRecordsWhereItWasStored:
    def test_the_destination_is_written_with_the_row(self, db, local_storage):
        """The captured identity is the provider's own answer, not a re-read of settings: only
        the instance that performs the `save` can speak for where it put the bytes."""
        profile_id = make_profile(db)

        resume_id = upload(db, profile_id, local_storage)

        resume = db.get(Resume, resume_id)
        assert recorded_identity(resume) == local_storage.identity()

    def test_the_revision_is_set_so_the_row_is_not_mistaken_for_a_legacy_one(
        self, db, local_storage
    ):
        """`local` has no bucket, so the revision is the only thing distinguishing a resume
        uploaded after capture from one uploaded before it. Without it every new local upload
        would read as legacy and be refused."""
        profile_id = make_profile(db)

        resume = db.get(Resume, upload(db, profile_id, local_storage))

        assert resume.storage_config_revision
        assert recorded_identity(resume) is not None

    def test_the_recorded_destination_is_the_one_the_file_is_readable_from(
        self, db, local_storage
    ):
        """End to end on one provider: what was written down is what the service will accept
        when it goes back for the bytes."""
        profile_id = make_profile(db)
        resume_id = upload(db, profile_id, local_storage, text="Specific resume text")

        result = resume_service.get_resume_file(db, resume_id, profile_id, local_storage)

        assert result is not None
        assert result[0].startswith(b"%PDF-")


class TestNoCredentialIsEverWrittenDown:
    def test_an_s3_destination_carries_no_access_key_or_secret(self, monkeypatch):
        """The whole reason `StorageIdentity` excludes credentials is that its fields reach
        database rows, logs and an operator's terminal. This asserts the exclusion against the
        real adapter rather than trusting the dataclass's documentation."""
        monkeypatch.setattr(settings, "storage_provider", "s3")
        monkeypatch.setattr(settings, "storage_s3_bucket", "talviora-resumes")
        monkeypatch.setattr(settings, "storage_s3_endpoint_url", "https://e.example.com")
        monkeypatch.setattr(settings, "storage_s3_region", "ap-south-1")
        monkeypatch.setattr(settings, "storage_s3_access_key_id", "AKIAVERYSECRETKEYID")
        monkeypatch.setattr(settings, "storage_s3_secret_access_key", "s3cr3t-do-not-store-me")
        provider = S3StorageProvider()
        provider._client = MagicMock()

        destination = resume_service.capture_destination(provider)

        recorded = [
            destination.provider,
            destination.bucket,
            destination.endpoint,
            destination.region,
            destination.expected_owner,
            destination.revision,
        ]
        for value in recorded:
            if value is None:
                continue
            assert "AKIAVERYSECRETKEYID" not in value
            assert "s3cr3t-do-not-store-me" not in value

    def test_the_fingerprint_does_not_leak_the_bucket_either(self, monkeypatch):
        """`revision` is printed in operator reports precisely so a bucket name does not have
        to be. A fingerprint that embedded the name would defeat that."""
        monkeypatch.setattr(settings, "storage_s3_bucket", "talviora-resumes")
        monkeypatch.setattr(settings, "storage_provider", "s3")
        monkeypatch.setattr(settings, "storage_s3_endpoint_url", "https://e.example.com")

        destination = StorageIdentity.from_settings()

        assert "talviora-resumes" not in destination.revision
        assert "e.example.com" not in destination.revision
        assert "talviora-resumes" not in destination.describe()


class TestAProviderThatCannotSayWhereItWrites:
    class Mute:
        """Saves happily but cannot state a destination."""

        name = "mute"

        def __init__(self):
            self.saved = []

        def identity(self):
            return None

        def save(self, key, data):
            self.saved.append(key)
            return key

        def load(self, key):  # pragma: no cover - not reached
            raise NotImplementedError

        def delete(self, key):  # pragma: no cover - not reached
            raise NotImplementedError

    def test_nothing_is_stored_at_all(self, db):
        """The capture happens *before* the save, so the refusal costs nothing. Storing first
        and refusing afterwards would leave an object on disk that no row points at -- the
        orphan this stage exists to stop creating."""
        profile_id = make_profile(db)
        storage = self.Mute()

        with pytest.raises(resume_service.ResumeStorageUnconfirmed) as caught:
            upload(db, profile_id, storage)

        assert caught.value.code == identity_module.IDENTITY_UNDETERMINABLE
        assert storage.saved == [], "the bytes must not be written before the destination is known"
        assert db.query(Resume).count() == 0, "and no row may be created either"

    def test_an_incomplete_destination_is_refused_too(self, db):
        """An `s3` identity with no bucket looks captured but cannot find the object again,
        which is worse than nothing recorded: every later request would treat it as
        confirmable."""

        class Vague(self.Mute):
            def identity(self):
                return StorageIdentity(provider="s3")

        profile_id = make_profile(db)
        storage = Vague()

        with pytest.raises(resume_service.ResumeStorageUnconfirmed) as caught:
            upload(db, profile_id, storage)

        assert caught.value.code == identity_module.IDENTITY_INCOMPLETE
        assert storage.saved == []
        assert db.query(Resume).count() == 0


class TestTheFileIsStoredButTheDatabaseFails:
    """The pre-existing failure mode, now pinned.

    `process_resume_upload` writes the object and then inserts the row. If the insert fails the
    bytes are already on disk, and there is no compensating delete -- the exception propagates
    and the object is orphaned. Stage B.2 does not change that, and these tests say so rather
    than implying otherwise: what they establish is the property that matters for this stage,
    that the failure cannot produce a *row* without a destination. An orphaned object with no
    row is recoverable by an operator sweeping storage; a row whose destination was never
    recorded is the state that needs a human to guess, which is exactly what this stage stops
    manufacturing.
    """

    def test_no_resume_row_survives_a_failed_commit(self, db, local_storage, monkeypatch):
        profile_id = make_profile(db)

        def explode():
            raise RuntimeError("the database went away")

        monkeypatch.setattr(db, "commit", explode)

        with pytest.raises(RuntimeError):
            upload(db, profile_id, local_storage)

        monkeypatch.undo()
        db.rollback()
        assert db.query(Resume).count() == 0

    def test_the_object_is_orphaned_and_that_is_the_known_gap(
        self, db, local_storage, monkeypatch
    ):
        """Documented, not asserted as desirable. The bytes are on disk with nothing pointing
        at them, which is a storage-cost and privacy concern rather than a correctness one --
        no student can reach it and no row claims it. Closing it needs either a compensating
        delete on the failure path or a storage sweep, and both are their own change.
        """
        from pathlib import Path

        # Captured BEFORE `monkeypatch.undo()`, and that is the whole correctness of this test.
        # `local_storage` points `settings.upload_dir` at a throwaway directory using the same
        # function-scoped `monkeypatch` this test requests, so `undo()` restores the real
        # configured directory as well as `db.commit`. Reading `settings.upload_dir` after the
        # undo therefore inspects the developer's actual uploads folder: on a machine with
        # leftover files there this passed while looking at nothing it had written, and on a
        # clean checkout it failed. Neither outcome said anything about the orphan.
        uploads = Path(settings.upload_dir)

        profile_id = make_profile(db)
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("gone")))

        with pytest.raises(RuntimeError):
            upload(db, profile_id, local_storage)

        monkeypatch.undo()
        db.rollback()
        written = list(uploads.rglob("*.pdf"))
        assert written, "the object really is left behind -- this is the gap, not a pass"
        assert db.query(Resume).count() == 0, "with no row pointing at it"


class TestEachUploadKeepsItsOwn:
    """There is no in-place replacement in this schema, and that is worth pinning.

    A student "replacing" a resume uploads a new one: `_save_upload` mints a fresh randomized
    key every time and nothing rewrites `file_path`, so no upload can overwrite or orphan an
    earlier object. Each row carries its own key *and* its own destination, which is what keeps
    the two independent when storage is reconfigured between uploads.
    """

    def test_two_uploads_get_different_keys(self, db, local_storage):
        profile_id = make_profile(db)

        # Ids first, rows afterwards: the second upload closes the session, which would detach
        # a Resume instance fetched before it.
        first_id = upload(db, profile_id, local_storage, text="first")
        second_id = upload(db, profile_id, local_storage, text="second")

        first = db.get(Resume, first_id)
        second = db.get(Resume, second_id)
        assert first.file_path != second.file_path
        assert first.id != second.id

    def test_a_reconfiguration_between_uploads_leaves_each_row_pointing_at_its_own(
        self, db, local_storage, monkeypatch, tmp_path
    ):
        """The destination is per row, not per profile and not per process. One student can
        legitimately have resumes in two buckets, and each must be read and erased from the one
        that holds it."""
        profile_id = make_profile(db)
        first_id = upload(db, profile_id, local_storage, text="before the move")

        # A second provider pointing somewhere else entirely.
        class Elsewhere(LocalStorageProvider):
            name = "local"

            def identity(self):
                return StorageIdentity(
                    provider="s3", bucket="bucket-b", endpoint="https://e.example.com"
                )

        monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "second"))
        second_id = upload(db, profile_id, Elsewhere(), text="after the move")

        first = db.get(Resume, first_id)
        second = db.get(Resume, second_id)
        assert recorded_identity(first) != recorded_identity(second)
        assert recorded_identity(first).provider == "local"
        assert recorded_identity(second).bucket == "bucket-b"

    def test_deleting_one_does_not_touch_the_other(self, db, local_storage):
        """The ordinary case, which the per-row destination must not break: two resumes on the
        same storage, one deleted."""
        profile_id = make_profile(db)
        first_id = upload(db, profile_id, local_storage, text="keep me")
        second_id = upload(db, profile_id, local_storage, text="delete me")

        assert resume_service.delete_resume(db, second_id, profile_id, local_storage) is True

        assert db.get(Resume, first_id) is not None
        assert db.get(Resume, second_id) is None
        assert (
            resume_service.get_resume_file(db, first_id, profile_id, local_storage) is not None
        )


class TestCrossUserAccessIsStillImpossible:
    def test_another_students_resume_is_not_readable_even_on_the_right_storage(
        self, db, local_storage
    ):
        """Ownership is checked before the destination, so a confirmed destination never
        becomes a way in. A matching identity says the file is reachable, never that the caller
        may reach it."""
        mine_id = make_profile(db)
        theirs_id = make_profile(db)
        their_resume_id = upload(db, theirs_id, local_storage, text="their resume")

        assert resume_service.get_resume_file(db, their_resume_id, mine_id, local_storage) is None
        assert resume_service.delete_resume(db, their_resume_id, mine_id, local_storage) is False
        assert db.get(Resume, their_resume_id) is not None

    def test_a_legacy_resume_of_another_student_is_a_plain_absence(self, db, local_storage):
        """Ordering matters here too: the answer must be "not found", not a 500 about storage.
        Otherwise the identity refusal would confirm that someone else's resume id exists."""
        mine_id = make_profile(db)
        theirs_id = make_profile(db)
        their_resume = db.get(Resume, upload(db, theirs_id, local_storage, text="theirs"))
        for column in IDENTITY_COLUMNS:
            setattr(their_resume, column, None)
        db.commit()

        assert resume_service.get_resume_file(db, their_resume.id, mine_id, local_storage) is None
        assert resume_service.delete_resume(db, their_resume.id, mine_id, local_storage) is False
