"""The state that makes the OAuth callback safe to expose without authentication.

The callback endpoint has no App Check header, no bearer token and no session -- GitHub's
redirect carries none of those. The state is therefore the entire access control for that route,
and each property below is one of the ways it can fail to be:

  guessable          -> an attacker completes an authorisation they never started
  stored in the raw  -> a database dump lets them do the same
  reusable           -> a captured callback URL can be replayed
  eternal            -> a captured callback URL can be replayed at leisure
  cross-provider     -> a state minted for one provider is spent at another's callback
  client-visible     -> the PKCE verifier leaks and the code exchange loses its second factor

Run against PostgreSQL as well as SQLite, because two of these depend on datetime comparison
semantics and those are exactly what differ between the two.
"""

import base64
import datetime
import hashlib

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.main  # noqa: F401  -- imports every model so create_all sees the full schema
from app.core.config import settings
from app.database.base import Base
from app.models.profile import StudentProfile
from app.models.provider_oauth_state import ProviderOAuthState
from app.models.user import User
from app.services.professional_presence import oauth_state
from tests import db_safety

# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())


@pytest.fixture()
def db():
    if _TEST_DATABASE_URL:
        engine = create_engine(_TEST_DATABASE_URL, poolclass=NullPool)
        db_safety.drop_all_safely(engine)
    else:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_connection, _record):
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
    profile = StudentProfile(user_id=user.id, full_name="Test Student")
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


class TestStateGeneration:
    def test_each_state_is_unique(self, db):
        profile = _profile(db, "unique@example.com")
        seen = {oauth_state.create_state(db, profile.id, "github")[0] for _ in range(20)}
        assert len(seen) == 20

    def test_the_state_carries_real_entropy(self, db):
        """256 bits from `secrets`. This is the only thing standing between an attacker and
        completing somebody's authorisation, so it is sized like a session token rather than
        like an identifier."""
        profile = _profile(db, "entropy@example.com")
        raw_state, _ = oauth_state.create_state(db, profile.id, "github")
        # token_urlsafe(32) renders 32 bytes as ~43 url-safe characters.
        assert len(raw_state) >= 40
        assert set(raw_state) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )

    def test_only_the_hash_of_the_state_is_stored(self, db):
        """A dump of this table must not let anyone finish a pending authorisation. Asserted
        across every column, so a future convenience column holding the raw value fails here."""
        profile = _profile(db, "hashed@example.com")
        raw_state, _ = oauth_state.create_state(db, profile.id, "github")

        row = db.query(ProviderOAuthState).one()
        assert row.state_hash == hashlib.sha256(raw_state.encode()).hexdigest()
        for column in ProviderOAuthState.__table__.columns:
            assert raw_state not in str(getattr(row, column.name)), f"raw state in {column.name}"

    def test_the_verifier_is_encrypted_at_rest_and_never_returned(self, db):
        """create_state returns the challenge, never the verifier. The caller therefore cannot
        leak the verifier even by accident, because it never holds it."""
        profile = _profile(db, "verifier@example.com")
        _, challenge = oauth_state.create_state(db, profile.id, "github")

        row = db.query(ProviderOAuthState).one()
        stored = row.code_verifier_encrypted
        from app.core import token_crypto

        verifier = token_crypto.decrypt(stored)
        assert verifier not in stored
        assert verifier != challenge
        assert oauth_state.code_challenge_for(verifier) == challenge


class TestPkce:
    def test_the_challenge_is_s256_of_the_verifier(self):
        """RFC 7636 S256: base64url(sha256(verifier)), padding stripped. Computed independently
        here rather than by calling the same helper, so a wrong implementation cannot agree with
        itself."""
        verifier = oauth_state.generate_code_verifier()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        assert oauth_state.code_challenge_for(verifier) == expected

    def test_the_challenge_is_not_the_verifier(self):
        """`plain` is what this rules out. GitHub rejects it, and a challenge equal to the
        verifier would mean anyone who captured the authorization URL held both halves."""
        verifier = oauth_state.generate_code_verifier()
        assert oauth_state.code_challenge_for(verifier) != verifier

    def test_the_challenge_carries_no_base64_padding(self):
        """A trailing '=' is not in the unreserved set and would be percent-encoded in the URL,
        which GitHub then compares against its own unpadded computation and rejects."""
        for _ in range(10):
            assert "=" not in oauth_state.code_challenge_for(oauth_state.generate_code_verifier())

    def test_the_verifier_length_is_inside_the_spec_range(self):
        """RFC 7636 requires 43..128 characters from the unreserved alphabet. Outside it, GitHub
        refuses the exchange -- after the student has already approved."""
        for _ in range(10):
            verifier = oauth_state.generate_code_verifier()
            assert 43 <= len(verifier) <= 128
            assert set(verifier) <= set(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
            )


class TestConsumption:
    def test_a_fresh_state_yields_its_student_and_verifier(self, db):
        profile = _profile(db, "consume@example.com")
        raw_state, challenge = oauth_state.create_state(db, profile.id, "github")

        profile_id, verifier = oauth_state.consume_state(db, raw_state, "github")

        assert profile_id == profile.id
        assert oauth_state.code_challenge_for(verifier) == challenge

    def test_consuming_twice_is_refused(self, db):
        """Replay: the same callback URL arriving a second time, from a refresh, a shared link,
        or a captured redirect."""
        profile = _profile(db, "replay@example.com")
        raw_state, _ = oauth_state.create_state(db, profile.id, "github")

        oauth_state.consume_state(db, raw_state, "github")
        with pytest.raises(oauth_state.ReplayedStateError):
            oauth_state.consume_state(db, raw_state, "github")

    def test_an_unknown_state_is_refused(self, db):
        _profile(db, "unknown@example.com")
        with pytest.raises(oauth_state.UnknownStateError):
            oauth_state.consume_state(db, "a-state-that-was-never-issued", "github")

    def test_an_expired_state_is_refused(self, db):
        """The datetime comparison this depends on is exactly what differs between SQLite and
        PostgreSQL, which is why this file runs against both."""
        profile = _profile(db, "expired@example.com")
        raw_state, _ = oauth_state.create_state(db, profile.id, "github")

        row = db.query(ProviderOAuthState).one()
        row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        db.commit()

        with pytest.raises(oauth_state.ExpiredStateError):
            oauth_state.consume_state(db, raw_state, "github")

    def test_a_state_minted_for_another_provider_is_refused(self, db):
        """Not reachable with one provider wired up, and implemented anyway: the moment LinkedIn
        exists, a state issued for it must not be spendable at GitHub's callback."""
        profile = _profile(db, "mismatch@example.com")
        raw_state, _ = oauth_state.create_state(db, profile.id, "linkedin")

        with pytest.raises(oauth_state.ProviderMismatchError):
            oauth_state.consume_state(db, raw_state, "github")

    def test_a_refused_consumption_does_not_spend_the_state(self, db):
        """A provider mismatch must not burn a state the student can still legitimately use --
        otherwise a stray request would cancel somebody's in-flight authorisation."""
        profile = _profile(db, "notspent@example.com")
        raw_state, _ = oauth_state.create_state(db, profile.id, "github")

        with pytest.raises(oauth_state.ProviderMismatchError):
            oauth_state.consume_state(db, raw_state, "linkedin")

        profile_id, _ = oauth_state.consume_state(db, raw_state, "github")
        assert profile_id == profile.id

    def test_consumption_is_recorded_rather_than_deleted(self, db):
        """Keeping the row is what lets a replay be told apart from a state that never existed.
        Deleting on use would make both look identical in the logs."""
        profile = _profile(db, "recorded@example.com")
        raw_state, _ = oauth_state.create_state(db, profile.id, "github")
        oauth_state.consume_state(db, raw_state, "github")

        row = db.query(ProviderOAuthState).one()
        assert row.consumed_at is not None

    def test_each_student_gets_their_own_binding(self, db):
        """The binding that makes a stolen callback useless. Two states in flight must resolve
        to their own students and never to each other's."""
        first = _profile(db, "bind-a@example.com")
        second = _profile(db, "bind-b@example.com")
        state_a, _ = oauth_state.create_state(db, first.id, "github")
        state_b, _ = oauth_state.create_state(db, second.id, "github")

        assert oauth_state.consume_state(db, state_a, "github")[0] == first.id
        assert oauth_state.consume_state(db, state_b, "github")[0] == second.id


class TestSweeping:
    def test_expired_rows_are_removed_and_live_ones_are_not(self, db):
        profile = _profile(db, "sweep@example.com")
        expired_state, _ = oauth_state.create_state(db, profile.id, "github")
        row = db.query(ProviderOAuthState).one()
        row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        db.commit()
        oauth_state.create_state(db, profile.id, "linkedin")

        removed = oauth_state.delete_expired(db)

        assert removed == 1
        assert db.query(ProviderOAuthState).count() == 1
        with pytest.raises(oauth_state.UnknownStateError):
            oauth_state.consume_state(db, expired_state, "github")
