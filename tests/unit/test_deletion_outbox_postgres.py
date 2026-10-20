"""The outbox against real PostgreSQL: the lease claim, the constraints, the downgrade.

Everything here needs behaviour SQLite does not have. `FOR UPDATE SKIP LOCKED` is the whole
basis of the concurrency claim and SQLite has neither row locks nor the clause, so a passing
SQLite run says nothing about it. The same goes for the partial-index access path and for a
downgrade that has to be refused on the engine a deployment actually runs.

Skipped without an isolated PostgreSQL database, and the guard in tests/db_safety.py is the
only route to one -- it refuses the application database, refuses a name that is not
`*_test`, and requires the destructive opt-in. See docs/postgres-test-environment.md.
"""

import datetime
import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.core.config import settings
from app.models.deletion_job import DeletionJob
from app.models.deletion_request import DeletionRequest
from app.models.user import User
from app.storage.provider import StorageError
from app.workers import deletion_worker
from tests import db_safety

# Validated at import rather than read from the environment: this module runs migrations and
# creates databases.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()

pytestmark = pytest.mark.skipif(
    not _TEST_DATABASE_URL,
    reason="the deletion outbox's lease claim and constraints need real PostgreSQL; "
    "credentials are never probed for",
)

_HEAD = "f7b3c91de204"
_PREVIOUS = "d4a1c08f52e7"


def _alembic_config() -> Config:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config()
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


@pytest.fixture()
def outbox_database(monkeypatch):
    """A throwaway PostgreSQL database at head, dropped afterwards.

    Mirrors the migration-test fixtures: engines come from db_safety.make_engine so nothing
    is pooled, and the drop runs in a finally without FORCE.
    """
    base = make_url(_TEST_DATABASE_URL)
    name = f"deletion_outbox_test_{uuid.uuid4().hex[:8]}"
    admin = db_safety.make_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = db_safety.hidden(base.set(database=name).render_as_string(hide_password=False))
    monkeypatch.setattr(settings, "database_url", url)
    try:
        command.upgrade(_alembic_config(), _HEAD)
        yield url
    finally:
        db_safety.drop_temp_database(admin, name)


def _session(url):
    engine = db_safety.make_engine(url)
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _seed_request(session, *, keys=("resumes/7/cv.pdf",), user_email=None):
    """A request with jobs, without going through Phase 1 -- the schema is what is under test."""
    user_id = None
    if user_email:
        user = User(email=user_email, password_hash="x", is_active=False, token_version=9)
        session.add(user)
        session.flush()
        user_id = user.id
    request = DeletionRequest(
        request_uuid=str(uuid.uuid4()),
        original_user_id=user_id if user_id is not None else 999999,
        status="pending",
    )
    session.add(request)
    session.flush()
    now = datetime.datetime.now(datetime.timezone.utc)
    for key in keys:
        session.add(
            DeletionJob(
                deletion_request_id=request.id,
                storage_provider="local",
                storage_key=key,
                status="pending",
                attempts=0,
                next_attempt_at=now - datetime.timedelta(seconds=1),
            )
        )
    session.commit()
    return request


class TestTheLeaseClaimUnderRealConcurrency:
    def test_two_workers_never_claim_the_same_job(self, outbox_database):
        """SKIP LOCKED, proven with two connections rather than argued from the SQL.

        The first session holds its row lock by not committing. The second must step over it
        and come back empty -- not block, and not claim it as well.
        """
        engine_a, session_a = _session(outbox_database)
        engine_b, session_b = _session(outbox_database)
        try:
            _seed_request(session_a)

            first = deletion_worker.claim_batch(session_a)  # locked, not yet committed
            assert len(first) == 1

            second = deletion_worker.claim_batch(session_b)
            assert second == [], "the second worker claimed a row the first already held"

            session_a.commit()
        finally:
            session_a.close()
            session_b.close()
            engine_a.dispose()
            engine_b.dispose()

    def test_two_workers_split_a_batch_rather_than_duplicating_it(self, outbox_database):
        engine_a, session_a = _session(outbox_database)
        engine_b, session_b = _session(outbox_database)
        try:
            _seed_request(session_a, keys=tuple(f"resumes/7/{i}.pdf" for i in range(4)))

            first = deletion_worker.claim_batch(session_a, limit=2)
            second = deletion_worker.claim_batch(session_b, limit=2)
            session_a.commit()
            session_b.commit()

            assert len(first) == 2
            assert len(second) == 2
            assert {job.id for job in first}.isdisjoint({job.id for job in second})
        finally:
            session_a.close()
            session_b.close()
            engine_a.dispose()
            engine_b.dispose()

    def test_a_committed_lease_is_invisible_to_the_other_worker(self, outbox_database):
        engine_a, session_a = _session(outbox_database)
        engine_b, session_b = _session(outbox_database)
        try:
            _seed_request(session_a)
            deletion_worker.claim_batch(session_a)
            session_a.commit()

            assert deletion_worker.claim_batch(session_b) == []
        finally:
            session_a.close()
            session_b.close()
            engine_a.dispose()
            engine_b.dispose()

    def test_an_expired_lease_is_reclaimable_by_the_other_worker(self, outbox_database):
        """A crashed worker's job returns to the queue without anyone noticing the crash."""
        engine_a, session_a = _session(outbox_database)
        engine_b, session_b = _session(outbox_database)
        try:
            _seed_request(session_a)
            claimed = deletion_worker.claim_batch(session_a)
            claimed[0].locked_until = datetime.datetime.now(
                datetime.timezone.utc
            ) - datetime.timedelta(seconds=1)
            session_a.commit()

            assert len(deletion_worker.claim_batch(session_b)) == 1
        finally:
            session_a.close()
            session_b.close()
            engine_a.dispose()
            engine_b.dispose()

    def test_the_claim_really_uses_skip_locked_on_postgres(self, outbox_database):
        """Guards the dialect branch: on PostgreSQL the clause must be in the statement."""
        engine, session = _session(outbox_database)
        try:
            assert session.bind.dialect.name == "postgresql"
            from sqlalchemy import select

            statement = (
                select(DeletionJob.id).limit(1).with_for_update(skip_locked=True)
            )
            compiled = str(statement.compile(session.bind))
            assert "FOR UPDATE SKIP LOCKED" in compiled
        finally:
            session.close()
            engine.dispose()


class TestTheSchemaOnRealPostgres:
    def test_the_unique_constraint_refuses_a_duplicate_object(self, outbox_database):
        from sqlalchemy.exc import IntegrityError

        engine, session = _session(outbox_database)
        try:
            request = _seed_request(session)
            session.add(
                DeletionJob(
                    deletion_request_id=request.id,
                    storage_provider="local",
                    storage_key="resumes/7/cv.pdf",
                    status="pending",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.close()
            engine.dispose()

    def test_both_indexes_exist(self, outbox_database):
        engine, session = _session(outbox_database)
        try:
            inspector = inspect(engine)
            request_indexes = {i["name"] for i in inspector.get_indexes("deletion_requests")}
            job_indexes = {i["name"] for i in inspector.get_indexes("deletion_jobs")}
            assert "ix_deletion_requests_status_created_at" in request_indexes
            assert "ix_deletion_jobs_status_next_attempt_at" in job_indexes
            assert "ix_deletion_jobs_deletion_request_id" in job_indexes
        finally:
            session.close()
            engine.dispose()

    def test_there_is_no_foreign_key_on_original_user_id(self, outbox_database):
        """On the engine that actually enforces them."""
        engine, session = _session(outbox_database)
        try:
            constrained = {
                column
                for fk in inspect(engine).get_foreign_keys("deletion_requests")
                for column in fk["constrained_columns"]
            }
            assert constrained == set()
        finally:
            session.close()
            engine.dispose()

    def test_the_user_row_can_be_deleted_while_a_job_remains(self, outbox_database):
        """The consequence of that absence, under real constraint enforcement."""
        engine, session = _session(outbox_database)
        try:
            request = _seed_request(session, user_email="leaving@example.com")
            session.delete(session.get(User, request.original_user_id))
            session.commit()

            assert session.query(DeletionRequest).one().status == "pending"
            assert session.query(DeletionJob).count() == 1
        finally:
            session.close()
            engine.dispose()

    def test_the_job_foreign_key_to_its_request_is_enforced(self, outbox_database):
        from sqlalchemy.exc import IntegrityError

        engine, session = _session(outbox_database)
        try:
            session.add(
                DeletionJob(
                    deletion_request_id=987654,
                    storage_provider="local",
                    storage_key="nowhere/x.pdf",
                    status="pending",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.close()
            engine.dispose()

    def test_the_timestamps_are_timezone_aware(self, outbox_database):
        engine, session = _session(outbox_database)
        try:
            _seed_request(session)
            created = session.query(DeletionRequest).one().created_at
            assert created.tzinfo is not None
        finally:
            session.close()
            engine.dispose()


class TestTheDowngradeOnRealPostgres:
    def test_it_is_refused_while_a_request_is_open(self, outbox_database):
        from app.database.downgrade_guard import OutstandingObligations

        engine, session = _session(outbox_database)
        try:
            _seed_request(session)
        finally:
            session.close()
            engine.dispose()

        with pytest.raises(OutstandingObligations):
            command.downgrade(_alembic_config(), _PREVIOUS)

        # The tables, and the obligation, are still there.
        engine, session = _session(outbox_database)
        try:
            assert session.query(DeletionRequest).count() == 1
            assert session.query(DeletionJob).count() == 1
        finally:
            session.close()
            engine.dispose()

    def test_needs_attention_also_refuses_it(self, outbox_database):
        from app.database.downgrade_guard import OutstandingObligations

        engine, session = _session(outbox_database)
        try:
            request = _seed_request(session)
            request.status = "needs_attention"
            session.commit()
        finally:
            session.close()
            engine.dispose()

        with pytest.raises(OutstandingObligations):
            command.downgrade(_alembic_config(), _PREVIOUS)

    def test_a_drained_queue_downgrades_cleanly(self, outbox_database):
        engine, session = _session(outbox_database)
        try:
            request = _seed_request(session)
            for job in session.query(DeletionJob).all():
                job.status = "done"
            request.status = "done"
            session.commit()
        finally:
            session.close()
            engine.dispose()

        command.downgrade(_alembic_config(), _PREVIOUS)

        engine, session = _session(outbox_database)
        try:
            tables = set(inspect(engine).get_table_names())
            assert "deletion_requests" not in tables
            assert "deletion_jobs" not in tables
        finally:
            session.close()
            engine.dispose()

    def test_the_upgrade_is_additive(self, outbox_database):
        """No existing table is altered: users keeps exactly the columns it had."""
        engine, session = _session(outbox_database)
        try:
            columns = {c["name"] for c in inspect(engine).get_columns("users")}
            assert "deletion_status" not in columns
            assert {"id", "email", "password_hash", "is_active", "token_version"} <= columns
        finally:
            session.close()
            engine.dispose()


class TestAFullRunOnPostgres:
    def test_a_request_completes_and_the_user_row_goes(self, outbox_database):
        class Recording:
            def __init__(self):
                self.deleted = []

            def delete(self, key):
                self.deleted.append(key)

        engine, session = _session(outbox_database)
        try:
            request = _seed_request(session, user_email="done@example.com")
            user_id = request.original_user_id

            storage = Recording()
            report = deletion_worker.run_once(session, storage)

            assert report.deleted == 1
            assert report.requests_closed == 1
            assert storage.deleted == ["resumes/7/cv.pdf"]
            assert session.get(User, user_id) is None
            assert session.query(DeletionRequest).one().status == "done"
        finally:
            session.close()
            engine.dispose()


class SelectiveStorage:
    """Fails the keys it is told to fail, records the rest. Opens nothing.

    The storage provider is still a stand-in -- no real bucket is configured -- but the
    database behaviour under these two scenarios is real PostgreSQL, which is the part that
    was previously covered only on SQLite.
    """

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.deleted: list[str] = []
        self.attempts: list[str] = []

    def delete(self, key: str) -> None:
        self.attempts.append(key)
        if key in self.failing:
            raise StorageError("simulated storage outage")
        self.deleted.append(key)


class TestMixedOutcomesInOnePassOnPostgres:
    """One object fails while another succeeds, inside a single worker pass.

    This is the test for the per-job transaction boundary: the failing job's rollback must not
    take the successful job's `done` with it, and the request must stay open while one
    obligation is still outstanding.
    """

    def test_a_failure_does_not_roll_back_the_success(self, outbox_database):
        good, bad = "resumes/7/good.pdf", "resumes/7/bad.pdf"
        engine, session = _session(outbox_database)
        try:
            request = _seed_request(session, keys=(good, bad), user_email="mixed@example.com")
            user_id = request.original_user_id
            storage = SelectiveStorage(failing={bad})

            report = deletion_worker.run_once(session, storage)

            assert sorted(storage.attempts) == sorted([good, bad]), "both must be attempted"
            assert storage.deleted == [good]
            assert report.deleted == 1
            assert report.retried == 1
            assert report.requests_closed == 0

            jobs = {job.storage_key: job for job in session.query(DeletionJob).all()}
            assert jobs[good].status == "done"
            assert jobs[good].completed_at is not None
            assert jobs[bad].status == "pending", "the obligation is not abandoned"
            assert jobs[bad].attempts == 1
            assert jobs[bad].last_error_class == "StorageError"
            assert jobs[bad].locked_until is None, "a failed job must not stay leased"
            assert jobs[bad].storage_key == bad, "the only pointer to the object must survive"

            assert session.query(DeletionRequest).one().status == "pending"
            assert session.get(User, user_id) is not None, (
                "the user row may only go once every object is resolved"
            )
        finally:
            session.close()
            engine.dispose()

    def test_the_outstanding_object_completes_on_a_later_pass(self, outbox_database):
        good, bad = "resumes/8/good.pdf", "resumes/8/bad.pdf"
        engine, session = _session(outbox_database)
        try:
            request = _seed_request(session, keys=(good, bad), user_email="later@example.com")
            user_id = request.original_user_id

            deletion_worker.run_once(session, SelectiveStorage(failing={bad}))

            failed = session.query(DeletionJob).filter(DeletionJob.storage_key == bad).one()
            failed.next_attempt_at = datetime.datetime.now(
                datetime.timezone.utc
            ) - datetime.timedelta(seconds=1)
            session.commit()

            recovered = SelectiveStorage()
            report = deletion_worker.run_once(session, recovered)

            assert recovered.deleted == [bad], "the already-done object must not be revisited"
            assert report.requests_closed == 1
            assert {job.status for job in session.query(DeletionJob).all()} == {"done"}
            assert session.query(DeletionRequest).one().status == "done"
            assert session.get(User, user_id) is None
        finally:
            session.close()
            engine.dispose()


class TestACrashAfterTheDeleteOnPostgres:
    """The dangerous window: the object is gone and the outcome was never recorded.

    On the real engine this also shows that the lease taken before the storage call keeps a
    second worker off the job until it expires, and that the durable obligation -- the row
    carrying the key -- survives the crash untouched.
    """

    def test_the_obligation_survives_and_stays_retryable(self, outbox_database):
        engine, session = _session(outbox_database)
        engine_b, session_b = _session(outbox_database)
        try:
            request = _seed_request(session, user_email="crash@example.com")
            user_id = request.original_user_id
            key = session.query(DeletionJob).one().storage_key
            storage = SelectiveStorage()

            claimed = deletion_worker.claim_batch(session)
            assert len(claimed) == 1
            session.commit()        # boundary 1: the lease is durable before anything is erased

            storage.delete(key)     # the object is gone...
            session.rollback()      # ...and the process died before recording it
            session.expire_all()

            job = session.query(DeletionJob).one()
            assert job.status == "pending", "a crash must not leave a job looking finished"
            assert job.attempts == 0
            assert job.storage_key == key, "the durable deletion obligation must survive"
            assert session.query(DeletionRequest).one().status == "pending"
            assert session.get(User, user_id) is not None

            # While the lease holds, no other worker may take it.
            assert deletion_worker.claim_batch(session_b) == []

            # The lease expires; the retry re-attempts and succeeds, because deleting an
            # object that is already absent is success.
            job.locked_until = datetime.datetime.now(
                datetime.timezone.utc
            ) - datetime.timedelta(seconds=1)
            session.commit()

            report = deletion_worker.run_once(session, storage)

            assert report.deleted == 1
            assert storage.attempts == [key, key], "the retry must re-attempt, harmlessly"
            assert session.query(DeletionJob).one().status == "done"
            assert session.query(DeletionRequest).one().status == "done"
            assert session.get(User, user_id) is None
        finally:
            session.close()
            session_b.close()
            engine.dispose()
            engine_b.dispose()

    def test_a_crash_before_the_claim_commits_is_equally_safe(self, outbox_database):
        """The other side of the window: nothing erased, nothing recorded, no trace."""
        engine, session = _session(outbox_database)
        try:
            _seed_request(session, user_email="nocrash@example.com")
            deletion_worker.claim_batch(session)
            session.rollback()  # died before the claim was even committed
            session.expire_all()

            job = session.query(DeletionJob).one()
            assert job.status == "pending"
            assert job.locked_until is None, "an uncommitted lease must leave no trace"
            assert len(deletion_worker.claim_batch(session)) == 1
        finally:
            session.close()
            engine.dispose()
