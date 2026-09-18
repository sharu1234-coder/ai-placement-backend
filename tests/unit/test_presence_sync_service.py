"""Phase 3: what a sync writes, what it refuses to write, and what a failure must not destroy.

The adapter is faked throughout. These tests are about the service's own guarantees --
idempotency, reconciliation, failure isolation and identity stability -- and driving them
through real HTTP would test httpx rather than any of that.
"""

import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core import token_crypto
from app.database.base import Base
from app.models.external_evidence_item import (
    SIGNAL_REPO_LANGUAGE,
    SIGNAL_REPO_PRIMARY_LANGUAGE,
    SIGNAL_REPO_TOPIC,
    ExternalEvidenceItem,
)
from app.models.external_profile_snapshot import ExternalProfileSnapshot
from app.models.profile import StudentProfile
from app.models.provider_connection import (
    PROVIDER_GITHUB,
    STATUS_ACTIVE,
    STATUS_NEEDS_REAUTH,
    SYNC_FAILED,
    SYNC_NEVER,
    SYNC_OK,
    ProviderConnection,
)
from app.models.user import User
from app.services.professional_presence import sync_service
from app.services.professional_presence.errors import (
    ProviderRateLimited,
    ProviderUnauthorized,
)
from app.services.professional_presence.providers.base import (
    ProviderFetch,
    ProviderProfile,
    ProviderRepository,
)

_ACCOUNT_ID = "4242"


def _profile(**overrides) -> ProviderProfile:
    base = dict(
        provider_user_id=_ACCOUNT_ID,
        username="priya",
        name="Priya R",
        bio="Building data tools",
        company=None,
        location=None,
        blog=None,
        public_repos=2,
        followers=3,
    )
    base.update(overrides)
    return ProviderProfile(**base)


def _repo(name: str, **overrides) -> ProviderRepository:
    base = dict(
        full_name=f"priya/{name}",
        name=name,
        description="A thing",
        primary_language="Python",
        languages=("Python",),
        topics=(),
        is_fork=False,
        is_archived=False,
        stars=0,
        pushed_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc),
    )
    base.update(overrides)
    return ProviderRepository(**base)


def _fetch(repos, complete: bool = True, profile=None) -> ProviderFetch:
    return ProviderFetch(
        profile=profile or _profile(), repositories=tuple(repos), complete=complete
    )


class _FakeAdapter:
    """Returns whatever it was handed, or raises whatever it was handed."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def fetch_account(self, access_token):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        # Foreign keys on, so the connection_id relationship is genuinely enforced here the way
        # a real PostgreSQL deployment enforces it.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_local()
    yield session
    session.close()


@pytest.fixture()
def connected(db_session, monkeypatch):
    """A student with a live, token-bearing GitHub connection."""
    monkeypatch.setattr(token_crypto, "is_configured", lambda: True)
    monkeypatch.setattr(token_crypto, "encrypt", lambda value: f"enc:{value}")
    monkeypatch.setattr(token_crypto, "decrypt", lambda value: value.replace("enc:", "", 1))

    user = User(email="priya@example.com", password_hash="x")
    db_session.add(user)
    db_session.flush()
    profile = StudentProfile(user_id=user.id)
    db_session.add(profile)
    db_session.flush()
    connection = ProviderConnection(
        profile_id=profile.id,
        provider=PROVIDER_GITHUB,
        provider_user_id=_ACCOUNT_ID,
        provider_username="priya",
        access_token_encrypted="enc:tok",
        status=STATUS_ACTIVE,
        sync_status=SYNC_NEVER,
    )
    db_session.add(connection)
    db_session.commit()
    return profile, connection


def _install(monkeypatch, result) -> _FakeAdapter:
    adapter = _FakeAdapter(result)
    monkeypatch.setattr(sync_service, "_adapter", lambda key: adapter)
    return adapter


def _evidence(db, connection_id):
    return db.query(ExternalEvidenceItem).filter(ExternalEvidenceItem.connection_id == connection_id).all()


class TestEvidenceIsWritten:
    def test_a_repository_language_becomes_evidence(self, db_session, connected, monkeypatch):
        profile, connection = connected
        _install(monkeypatch, _fetch([_repo("bus-tracker")]))

        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        rows = _evidence(db_session, connection.id)
        assert [(r.skill, r.signal_kind) for r in rows] == [("Python", SIGNAL_REPO_PRIMARY_LANGUAGE)]
        assert rows[0].source_ref == "github:repo:priya/bus-tracker"

    def test_secondary_languages_and_topics_are_kept_apart(self, db_session, connected, monkeypatch):
        profile, connection = connected
        _install(
            monkeypatch,
            _fetch([_repo("api", languages=("Python", "SQL"), topics=("kafka",))]),
        )

        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        kinds = {(r.skill, r.signal_kind) for r in _evidence(db_session, connection.id)}
        assert ("Python", SIGNAL_REPO_PRIMARY_LANGUAGE) in kinds
        assert ("SQL", SIGNAL_REPO_LANGUAGE) in kinds
        # A topic is the student's own label and must stay distinguishable from a measurement.
        assert ("kafka", SIGNAL_REPO_TOPIC) in kinds

    def test_observed_at_is_the_push_time_not_the_sync_time(self, db_session, connected, monkeypatch):
        """Staleness has to mean "this code is old", never "we synced a while ago"."""
        profile, connection = connected
        pushed = datetime.datetime(2024, 1, 5, tzinfo=datetime.timezone.utc)
        _install(monkeypatch, _fetch([_repo("old", pushed_at=pushed)]))

        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        observed = _evidence(db_session, connection.id)[0].observed_at
        assert observed.year == 2024

    def test_forks_and_archives_produce_no_evidence(self, db_session, connected, monkeypatch):
        """A fork is somebody else's code, and an archive is explicitly finished work."""
        profile, connection = connected
        _install(
            monkeypatch,
            _fetch([_repo("forked", is_fork=True), _repo("done", is_archived=True)]),
        )

        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        assert _evidence(db_session, connection.id) == []

    def test_connection_records_a_successful_sync(self, db_session, connected, monkeypatch):
        profile, connection = connected
        _install(monkeypatch, _fetch([_repo("a")]))

        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        db_session.refresh(connection)
        assert connection.sync_status == SYNC_OK
        assert connection.last_synced_at is not None
        assert connection.last_error_kind is None


class TestIdempotency:
    def test_syncing_twice_does_not_duplicate_evidence(self, db_session, connected, monkeypatch):
        """Breadth is counted in distinct sources, so duplicates would inflate confidence."""
        profile, connection = connected
        _install(monkeypatch, _fetch([_repo("api", languages=("Python", "SQL"), topics=("etl",))]))

        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)
        first = len(_evidence(db_session, connection.id))
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        assert len(_evidence(db_session, connection.id)) == first

    def test_an_unchanged_account_writes_no_second_snapshot(self, db_session, connected, monkeypatch):
        """Opening the screen must not cost a history row -- only a real change should."""
        profile, connection = connected
        _install(monkeypatch, _fetch([_repo("a")]))

        first = sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)
        second = sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        assert first.snapshot_written is True
        assert second.snapshot_written is False
        assert db_session.query(ExternalProfileSnapshot).filter_by(connection_id=connection.id).count() == 1

    def test_a_real_change_appends_a_snapshot(self, db_session, connected, monkeypatch):
        profile, connection = connected
        adapter = _install(monkeypatch, _fetch([_repo("a", description=None)]))
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        adapter.result = _fetch([_repo("a", description="Now described")])
        result = sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        assert result.snapshot_written is True
        rows = (
            db_session.query(ExternalProfileSnapshot)
            .filter_by(connection_id=connection.id)
            .order_by(ExternalProfileSnapshot.sequence)
            .all()
        )
        assert [r.sequence for r in rows] == [1, 2]


class TestReconciliation:
    def test_a_deleted_repository_loses_its_evidence(self, db_session, connected, monkeypatch):
        profile, connection = connected
        adapter = _install(monkeypatch, _fetch([_repo("keep"), _repo("gone")]))
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)
        assert len(_evidence(db_session, connection.id)) == 2

        adapter.result = _fetch([_repo("keep")])
        result = sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        refs = {r.source_ref for r in _evidence(db_session, connection.id)}
        assert refs == {"github:repo:priya/keep"}
        assert result.evidence_removed == 1

    def test_an_incomplete_listing_never_removes_evidence(self, db_session, connected, monkeypatch):
        """The dangerous case: a student past the page cap must not lose real evidence
        just because we stopped reading."""
        profile, connection = connected
        adapter = _install(monkeypatch, _fetch([_repo("keep"), _repo("unseen")]))
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        adapter.result = _fetch([_repo("keep")], complete=False)
        result = sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        assert result.evidence_removed == 0
        assert len(_evidence(db_session, connection.id)) == 2

    def test_a_repository_turned_into_a_fork_stops_being_evidence(self, db_session, connected, monkeypatch):
        profile, connection = connected
        adapter = _install(monkeypatch, _fetch([_repo("a")]))
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        adapter.result = _fetch([_repo("a", is_fork=True)])
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        assert _evidence(db_session, connection.id) == []


class TestFailureDestroysNothing:
    def test_a_provider_failure_keeps_existing_evidence(self, db_session, connected, monkeypatch):
        """"GitHub is down" must never be rendered as "you have no evidence"."""
        profile, connection = connected
        adapter = _install(monkeypatch, _fetch([_repo("a")]))
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)
        before = len(_evidence(db_session, connection.id))

        adapter.result = ProviderRateLimited()
        with pytest.raises(ProviderRateLimited):
            sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        assert len(_evidence(db_session, connection.id)) == before

    def test_a_failure_does_not_advance_last_synced_at(self, db_session, connected, monkeypatch):
        """Otherwise "synced 2 minutes ago" sits beside weeks-old data."""
        profile, connection = connected
        adapter = _install(monkeypatch, _fetch([_repo("a")]))
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)
        db_session.refresh(connection)
        good = connection.last_synced_at

        adapter.result = ProviderRateLimited()
        with pytest.raises(ProviderRateLimited):
            sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        db_session.refresh(connection)
        assert connection.last_synced_at == good
        assert connection.sync_status == SYNC_FAILED
        assert connection.last_error_kind == "rate_limit"

    def test_a_rejected_credential_asks_for_reconnection(self, db_session, connected, monkeypatch):
        """"Reconnect GitHub" and "try again" are different instructions."""
        profile, connection = connected
        _install(monkeypatch, ProviderUnauthorized())

        with pytest.raises(ProviderUnauthorized):
            sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        db_session.refresh(connection)
        assert connection.status == STATUS_NEEDS_REAUTH

    def test_a_recovered_connection_becomes_active_again(self, db_session, connected, monkeypatch):
        profile, connection = connected
        adapter = _install(monkeypatch, ProviderUnauthorized())
        with pytest.raises(ProviderUnauthorized):
            sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        adapter.result = _fetch([_repo("a")])
        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        db_session.refresh(connection)
        assert connection.status == STATUS_ACTIVE
        assert connection.sync_status == SYNC_OK


class TestIdentityStability:
    def test_a_token_returning_a_different_account_is_refused(self, db_session, connected, monkeypatch):
        """The account-takeover shaped bug: continuing would file one person's repositories
        under another person's profile."""
        profile, connection = connected
        _install(monkeypatch, _fetch([_repo("a")], profile=_profile(provider_user_id="9999")))

        with pytest.raises(sync_service.ConnectionNotSyncableError):
            sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        assert _evidence(db_session, connection.id) == []
        db_session.refresh(connection)
        assert connection.status == STATUS_NEEDS_REAUTH

    def test_a_renamed_login_is_accepted_and_refreshed(self, db_session, connected, monkeypatch):
        """A login can be renamed by its owner; the numeric id is what identity is keyed on."""
        profile, connection = connected
        _install(monkeypatch, _fetch([_repo("a")], profile=_profile(username="priya-r")))

        sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)

        db_session.refresh(connection)
        assert connection.provider_username == "priya-r"


class TestPreconditions:
    def test_syncing_without_a_connection_is_refused(self, db_session, connected, monkeypatch):
        """A second student, with no connection of their own, gets nothing -- and in particular
        does not pick up the connected student's."""
        _install(monkeypatch, _fetch([]))
        other_user = User(email="arjun@example.com", password_hash="x")
        db_session.add(other_user)
        db_session.flush()
        other = StudentProfile(user_id=other_user.id)
        db_session.add(other)
        db_session.flush()

        with pytest.raises(sync_service.ConnectionNotFoundError):
            sync_service.sync(db_session, other.id, PROVIDER_GITHUB)

    def test_a_connection_without_a_token_asks_for_reconnection(self, db_session, connected, monkeypatch):
        profile, connection = connected
        connection.access_token_encrypted = None
        db_session.commit()
        _install(monkeypatch, _fetch([_repo("a")]))

        with pytest.raises(sync_service.ConnectionNotSyncableError):
            sync_service.sync(db_session, profile.id, PROVIDER_GITHUB)
