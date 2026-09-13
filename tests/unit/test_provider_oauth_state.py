"""What the OAuth state table has to make possible before any OAuth code is written.

Scope note, so these tests are not read as claiming more than they check: the callback flow
itself does not exist yet (Phase 2). What is asserted here is that the *persistence layer*
can support a single-use, expiring, user-bound state -- that the columns exist, that the
database rejects a duplicate state, and that "still usable" is expressible as a query rather
than as a convention someone has to remember. A test that the callback actually refuses a
replayed state belongs with the callback.

The distinction matters because the usual way this goes wrong is not a missing column. It is a
flow that looks correct, reads `consumed_at`, and then forgets to write it -- or writes it
after the exchange rather than before. Those are Phase 2 failures. This file's job is to make
sure Phase 2 has something honest to build on, and in particular that the raw state value has
nowhere to be stored even if someone later wants to store it.
"""

import datetime
import hashlib
import os

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.main  # noqa: F401  -- imports every model so create_all sees the full schema
from app.database.base import Base
from app.models.profile import StudentProfile
from app.models.provider_oauth_state import ProviderOAuthState
from app.models.user import User

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# Timezone-aware, and every comparison below is made in SQL rather than in Python.
#
# Both details are load-bearing. These columns are timestamptz precisely because an earlier
# naive version of them failed here first: against a PostgreSQL server set to Asia/Calcutta, an
# aware UTC value written to a naive column came back five and a half hours later, and
# `test_an_expired_state_is_not_usable` passed on SQLite while failing on the database
# production actually runs. Comparing in SQL keeps the rule in one place and lets each dialect
# apply its own semantics, instead of Python comparing a value whose tzinfo differs by backend.
_NOW = datetime.datetime(2026, 9, 13, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def db():
    if _TEST_DATABASE_URL:
        engine = create_engine(_TEST_DATABASE_URL, poolclass=NullPool)
        Base.metadata.drop_all(bind=engine)
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


def _state(profile_id: int, raw_state: str, **overrides) -> ProviderOAuthState:
    fields = {
        "state_hash": hashlib.sha256(raw_state.encode()).hexdigest(),
        "profile_id": profile_id,
        "provider": "github",
        "code_verifier_encrypted": "gAAAAA-ciphertext-stand-in",
        "expires_at": _NOW + datetime.timedelta(minutes=10),
    }
    fields.update(overrides)
    return ProviderOAuthState(**fields)


def _usable(db, raw_state: str, now: datetime.datetime):
    """The exact predicate the callback will use: right state, not expired, not already spent.

    Written here as one query so that the three conditions live together. Splitting them across
    call sites is how one of them ends up forgotten.
    """
    return (
        db.query(ProviderOAuthState)
        .filter(
            ProviderOAuthState.state_hash == hashlib.sha256(raw_state.encode()).hexdigest(),
            ProviderOAuthState.expires_at > now,
            ProviderOAuthState.consumed_at.is_(None),
        )
        .one_or_none()
    )


class TestStateStorage:
    def test_a_state_persists_unconsumed(self, db):
        profile = _profile(db, "state@example.com")
        db.add(_state(profile.id, "raw-state-value"))
        db.commit()

        stored = db.query(ProviderOAuthState).one()
        assert stored.consumed_at is None
        assert stored.provider == "github"
        assert stored.profile_id == profile.id

    def test_the_raw_state_value_is_never_stored(self, db):
        """The state travels through the system browser, a redirect chain and very likely a
        proxy access log, so it must be assumed to leak. Storing only its hash means a dump of
        this table cannot be used to finish somebody's pending authorisation.

        Asserted against every column rather than against `state_hash` alone, so that adding a
        convenience column later that happens to hold the raw value fails here.
        """
        profile = _profile(db, "no-raw@example.com")
        raw = "raw-state-value-that-must-not-be-stored"
        db.add(_state(profile.id, raw))
        db.commit()

        stored = db.query(ProviderOAuthState).one()
        for column in ProviderOAuthState.__table__.columns:
            value = getattr(stored, column.name)
            assert raw not in str(value), f"raw state leaked into {column.name}"
        assert stored.state_hash == hashlib.sha256(raw.encode()).hexdigest()

    def test_a_state_must_belong_to_a_real_profile(self, db):
        """The binding that makes a stolen callback useless. The callback arrives with no
        session of its own, so this row is the only thing that says whose authorisation it is."""
        db.add(_state(profile_id=999_999, raw_state="orphan"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_two_live_states_cannot_share_a_hash(self, db):
        """A collision would be exactly the ambiguity an attacker wants: one lookup resolving
        to either of two students' pending authorisations."""
        first = _profile(db, "collide-a@example.com")
        second = _profile(db, "collide-b@example.com")

        db.add(_state(first.id, "same-state"))
        db.commit()
        db.add(_state(second.id, "same-state"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestExpiryAndSingleUse:
    def test_a_fresh_state_is_usable(self, db):
        profile = _profile(db, "fresh@example.com")
        db.add(_state(profile.id, "fresh-state"))
        db.commit()

        assert _usable(db, "fresh-state", _NOW) is not None

    def test_an_expired_state_is_not_usable(self, db):
        profile = _profile(db, "expired@example.com")
        db.add(_state(profile.id, "old-state", expires_at=_NOW - datetime.timedelta(minutes=1)))
        db.commit()

        assert _usable(db, "old-state", _NOW) is None

    def test_a_consumed_state_is_not_usable_again(self, db):
        """Replay. The same callback URL arriving twice -- a refresh, a shared link, a captured
        redirect -- must not mint a second connection."""
        profile = _profile(db, "replay@example.com")
        db.add(_state(profile.id, "one-shot"))
        db.commit()

        state = _usable(db, "one-shot", _NOW)
        assert state is not None
        state.consumed_at = _NOW
        db.commit()

        assert _usable(db, "one-shot", _NOW) is None

    def test_consumption_does_not_delete_the_row(self, db):
        """Deleting on consumption would make a replay indistinguishable from a state that
        never existed, and both would have to be reported as the same generic failure. Keeping
        the row means the two can be told apart, which matters when diagnosing a real attack."""
        profile = _profile(db, "kept@example.com")
        db.add(_state(profile.id, "kept-state"))
        db.commit()

        state = db.query(ProviderOAuthState).one()
        state.consumed_at = _NOW
        db.commit()

        assert db.query(ProviderOAuthState).count() == 1

    def test_expired_states_are_findable_for_sweeping(self, db):
        """Rows outlive their usefulness by minutes and would otherwise accumulate forever.
        `expires_at` is indexed so this stays a range scan rather than a full table scan."""
        profile = _profile(db, "sweep@example.com")
        db.add(_state(profile.id, "stale-1", expires_at=_NOW - datetime.timedelta(hours=2)))
        db.commit()
        db.add(_state(profile.id, "live-1"))
        db.commit()

        expired = (
            db.query(ProviderOAuthState)
            .filter(ProviderOAuthState.expires_at < _NOW)
            .all()
        )
        assert len(expired) == 1
