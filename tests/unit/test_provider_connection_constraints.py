"""The uniqueness rules that decide who owns an external identity.

Two of the assertions here are the reason this file exists rather than being folded into a
model smoke test:

  * one live connection per (profile, provider) -- so a student cannot accumulate three
    simultaneous GitHub grants and leave the sync path guessing which token to use;
  * one live connection per (provider, provider_user_id) -- so two students cannot both claim
    the same GitHub account, which would let one of them present another person's public
    repositories as evidence for their own career readiness.

Both are *partial* unique indexes, predicated on `revoked_at IS NULL`, and the partiality is
itself load-bearing: a plain unique constraint would let a student connect GitHub exactly once
ever, because the revoked row would keep occupying the pair forever. So the tests come in
pairs -- the constraint rejects a duplicate while the first connection is live, and accepts the
same insert once the first has been revoked.

These run on whatever database the suite is pointed at. On PostgreSQL, which production uses,
the partial index is native. On SQLite the model and migration both pass `sqlite_where`
alongside `postgresql_where` -- SQLite has supported partial indexes since 3.8.0 -- so the
development database enforces the same rule rather than a stricter one. Running this file under
TEST_DATABASE_URL is what actually proves the production behaviour; the SQLite run proves the
two databases agree.
"""

import datetime
import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.main  # noqa: F401  -- imports every model so create_all sees the full schema
from app.core import token_crypto
from app.core.config import settings
from app.database.base import Base
from app.models.external_evidence_item import ExternalEvidenceItem
from app.models.external_profile_snapshot import ExternalProfileSnapshot
from app.models.profile import StudentProfile
from app.models.provider_connection import ProviderConnection
from app.models.user import User

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def db():
    if _TEST_DATABASE_URL:
        engine = create_engine(_TEST_DATABASE_URL, poolclass=NullPool)
        Base.metadata.drop_all(bind=engine)
    else:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_connection, _record):
            # Without this SQLite ignores foreign keys entirely, and the ownership tests below
            # would pass while asserting nothing at all.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _profile(db, email: str) -> StudentProfile:
    user = User(email=email, password_hash="hashed")
    db.add(user)
    db.commit()
    db.refresh(user)
    profile = StudentProfile(user_id=user.id, full_name=email.split("@")[0])
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _connection(profile_id: int, provider_user_id: str = "583231", **overrides) -> ProviderConnection:
    fields = {
        "profile_id": profile_id,
        "provider": "github",
        "provider_user_id": provider_user_id,
        "provider_username": "octocat",
        "granted_scopes": "",
    }
    fields.update(overrides)
    return ProviderConnection(**fields)


def _revoked_at() -> datetime.datetime:
    return datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.timezone.utc)


class TestConnectionCreation:
    def test_a_connection_persists_with_the_expected_defaults(self, db):
        profile = _profile(db, "creation@example.com")
        db.add(_connection(profile.id))
        db.commit()

        stored = db.query(ProviderConnection).one()
        assert stored.provider == "github"
        assert stored.provider_user_id == "583231"
        assert stored.status == "active"
        assert stored.sync_status == "never"
        assert stored.revoked_at is None
        assert stored.last_synced_at is None
        assert stored.last_error_kind is None
        # Empty string, not null: the minimum-permission choice for V1 requests no GitHub
        # scope at all, and "we asked for nothing" has to be distinguishable from "we never
        # recorded what we asked for".
        assert stored.granted_scopes == ""

    def test_a_connection_cannot_reference_a_profile_that_does_not_exist(self, db):
        db.add(_connection(profile_id=999_999))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestOneLiveConnectionPerProvider:
    def test_a_second_live_github_connection_is_rejected(self, db):
        profile = _profile(db, "one-live@example.com")
        db.add(_connection(profile.id))
        db.commit()

        db.add(_connection(profile.id, provider_user_id="999999"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_the_same_student_may_connect_a_different_provider(self, db):
        profile = _profile(db, "two-providers@example.com")
        db.add(_connection(profile.id))
        db.commit()

        db.add(_connection(profile.id, provider="linkedin", provider_user_id="urn:li:person:abc"))
        db.commit()

        assert db.query(ProviderConnection).count() == 2

    def test_revoking_frees_the_slot_so_a_student_can_reconnect(self, db):
        """The whole reason the index is partial. Without `WHERE revoked_at IS NULL`, a student
        who disconnected GitHub once could never connect it again."""
        profile = _profile(db, "reconnect@example.com")
        first = _connection(profile.id)
        db.add(first)
        db.commit()

        first.revoked_at = _revoked_at()
        first.status = "revoked"
        db.commit()

        db.add(_connection(profile.id))
        db.commit()

        assert db.query(ProviderConnection).count() == 2
        live = db.query(ProviderConnection).filter(ProviderConnection.revoked_at.is_(None)).all()
        assert len(live) == 1

    def test_any_number_of_revoked_rows_may_coexist(self, db):
        """History accumulates freely -- only the live slot is exclusive. A student who
        connects and disconnects repeatedly must not eventually be locked out."""
        profile = _profile(db, "history@example.com")
        for _ in range(3):
            row = _connection(profile.id)
            db.add(row)
            db.commit()
            row.revoked_at = _revoked_at()
            row.status = "revoked"
            db.commit()

        db.add(_connection(profile.id))
        db.commit()

        assert db.query(ProviderConnection).count() == 4


class TestOneLiveConnectionPerExternalIdentity:
    def test_two_students_cannot_both_hold_the_same_github_account(self, db):
        """The security-relevant one. Without this, a student could connect a GitHub account
        that is not theirs -- one already connected by someone else -- and its public
        repositories would be counted as evidence for both careers."""
        first = _profile(db, "identity-a@example.com")
        second = _profile(db, "identity-b@example.com")

        db.add(_connection(first.id, provider_user_id="583231"))
        db.commit()

        db.add(_connection(second.id, provider_user_id="583231"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_giving_up_an_identity_releases_it_for_someone_else(self, db):
        """A real case, not a hypothetical: a shared or reassigned GitHub account, or a student
        who connected the wrong one. Once the first grant is revoked the identity is free."""
        first = _profile(db, "release-a@example.com")
        second = _profile(db, "release-b@example.com")

        original = _connection(first.id, provider_user_id="583231")
        db.add(original)
        db.commit()

        original.revoked_at = _revoked_at()
        original.status = "revoked"
        db.commit()

        db.add(_connection(second.id, provider_user_id="583231"))
        db.commit()

        assert db.query(ProviderConnection).count() == 2

    def test_the_same_id_on_a_different_provider_is_not_a_collision(self, db):
        """The identity rule is scoped by provider. GitHub user 583231 and a LinkedIn subject
        that happens to serialise identically are different people."""
        first = _profile(db, "scoped-a@example.com")
        second = _profile(db, "scoped-b@example.com")

        db.add(_connection(first.id, provider_user_id="583231"))
        db.commit()
        db.add(_connection(second.id, provider="linkedin", provider_user_id="583231"))
        db.commit()

        assert db.query(ProviderConnection).count() == 2


class TestEvidenceOwnership:
    def test_evidence_cannot_exist_without_a_connection(self, db):
        db.add(ExternalEvidenceItem(
            connection_id=999_999, skill="Python", signal_kind="repo_primary_language",
            source_ref="github:repo:ghost/repo",
            observed_at=datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_resyncing_the_same_signal_does_not_duplicate_it(self, db):
        """Breadth in the Evidence Engine is counted in distinct sources, so duplicated rows
        would inflate a student's confidence every time a sync ran."""
        profile = _profile(db, "resync@example.com")
        connection = _connection(profile.id)
        db.add(connection)
        db.commit()
        db.refresh(connection)

        observed = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        for _ in range(2):
            db.add(ExternalEvidenceItem(
                connection_id=connection.id, skill="Python", signal_kind="repo_primary_language",
                source_ref="github:repo:octocat/hello-world", observed_at=observed,
            ))
            if _ == 0:
                db.commit()

        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.query(ExternalEvidenceItem).count() == 1

    def test_the_same_skill_from_two_repositories_is_two_signals(self, db):
        """source_ref is part of the identity precisely so this stays true: two repositories
        both written in Python are genuinely two observations, not a duplicate."""
        profile = _profile(db, "two-repos@example.com")
        connection = _connection(profile.id)
        db.add(connection)
        db.commit()
        db.refresh(connection)

        observed = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        db.add(ExternalEvidenceItem(
            connection_id=connection.id, skill="Python", signal_kind="repo_primary_language",
            source_ref="github:repo:octocat/one", observed_at=observed,
        ))
        db.add(ExternalEvidenceItem(
            connection_id=connection.id, skill="Python", signal_kind="repo_primary_language",
            source_ref="github:repo:octocat/two", observed_at=observed,
        ))
        db.commit()

        assert db.query(ExternalEvidenceItem).count() == 2


class TestSnapshotSequence:
    def test_a_sequence_number_cannot_repeat_within_a_connection(self, db):
        """Ordering history by created_at would be a guess: two concurrent syncs can share a
        wall-clock timestamp. The unique constraint makes the order a fact, and turns a
        concurrent double-write into an IntegrityError the service can recover from."""
        profile = _profile(db, "sequence@example.com")
        connection = _connection(profile.id)
        db.add(connection)
        db.commit()
        db.refresh(connection)

        db.add(ExternalProfileSnapshot(
            connection_id=connection.id, sequence=1, fingerprint="a" * 64, payload={"public_repos": 4},
        ))
        db.commit()
        db.add(ExternalProfileSnapshot(
            connection_id=connection.id, sequence=1, fingerprint="b" * 64, payload={"public_repos": 5},
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_the_same_fingerprint_may_recur_within_a_connection(self, db):
        """Deliberately NOT unique. A student whose profile goes A -> B -> A has genuinely
        returned to a previous state, and history must be able to record that rather than
        rejecting the write. Change detection compares against the most recent snapshot, which
        is why it needs no uniqueness here."""
        profile = _profile(db, "fingerprint@example.com")
        connection = _connection(profile.id)
        db.add(connection)
        db.commit()
        db.refresh(connection)

        for sequence in (1, 2):
            db.add(ExternalProfileSnapshot(
                connection_id=connection.id, sequence=sequence, fingerprint="a" * 64,
                payload={"public_repos": 4},
            ))
        db.commit()

        assert db.query(ExternalProfileSnapshot).count() == 2


class TestCredentialStorage:
    """A stored credential must be ciphertext in the column, not merely "encrypted somewhere".

    These assert against the value as it actually sits in the row -- read back through a fresh
    query rather than off the in-memory object -- because the mistake this catches is assigning
    the plaintext to the column and encrypting only on the way out.
    """

    def test_an_access_token_is_stored_as_ciphertext(self, db, monkeypatch):
        monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
        plaintext = "gho_ThisIsNotARealGitHubTokenJustATestValue"

        profile = _profile(db, "ciphertext@example.com")
        db.add(_connection(profile.id, access_token_encrypted=token_crypto.encrypt(plaintext)))
        db.commit()
        db.expire_all()

        stored = db.query(ProviderConnection).one()
        assert stored.access_token_encrypted != plaintext
        assert token_crypto.decrypt(stored.access_token_encrypted) == plaintext

    def test_no_column_on_the_row_contains_the_plaintext_token(self, db, monkeypatch):
        """The leaked-backup property, asserted across every column rather than just the one we
        meant to encrypt -- so a future column that happens to cache the token fails here."""
        monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
        plaintext = "gho_ThisIsNotARealGitHubTokenJustATestValue"

        profile = _profile(db, "no-plaintext@example.com")
        db.add(_connection(profile.id, access_token_encrypted=token_crypto.encrypt(plaintext)))
        db.commit()
        db.expire_all()

        stored = db.query(ProviderConnection).one()
        for column in ProviderConnection.__table__.columns:
            value = getattr(stored, column.name)
            assert plaintext not in str(value), f"plaintext token leaked into {column.name}"

    def test_a_connection_may_outlive_its_credential(self, db):
        """Disconnecting destroys the token while keeping the row as the audit record that a
        grant once existed -- so the credential columns have to be nullable, and a row with no
        token is a valid state rather than a broken one."""
        profile = _profile(db, "revoked-token@example.com")
        connection = _connection(profile.id, access_token_encrypted="gAAAAA-ciphertext")
        db.add(connection)
        db.commit()

        connection.access_token_encrypted = None
        connection.refresh_token_encrypted = None
        connection.revoked_at = _revoked_at()
        connection.status = "revoked"
        db.commit()
        db.expire_all()

        stored = db.query(ProviderConnection).one()
        assert stored.access_token_encrypted is None
        assert stored.revoked_at is not None
