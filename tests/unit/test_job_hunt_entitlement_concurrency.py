"""Two requests at once, and the one free search between them.

This is the case the whole design of app/services/job_hunt/entitlement.py is shaped around, and
the one that a "read the flag, then set it" implementation gets wrong every time under load
while passing every sequential test.

Two things about the setup are load-bearing.

*Real threads on real sessions.* Calling claim() twice in a row proves nothing about a race;
the failure mode is specifically two transactions overlapping, so each thread gets its own
Session and they are released together by a Barrier.

*A file-backed database, never conftest's in-memory one.* That fixture uses StaticPool, which
hands every session the SAME connection -- so two "concurrent" sessions would share one
transaction, each would see the other's uncommitted writes, and the test would pass no matter
what the code did. This exact false pass has already been observed in this repository once
(see tests/unit/test_job_hunt_concurrency.py), which is why it is spelled out again here.

SQLite serialises writers, so what is being demonstrated is not parallel execution -- it is
that the *outcome* is decided by the unique constraint rather than by a Python-side check, and
therefore holds however the two transactions happen to interleave. On PostgreSQL the same
constraint does the same job with genuine parallelism.
"""

import datetime
import threading
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- registers every model with Base.metadata
from app.database.base import Base
from app.models.job_hunt_usage import JobHuntUsage
from app.models.profile import StudentProfile
from app.models.user import User
from app.services.job_hunt import entitlement
from tests.fixtures.premium_helpers import grant_premium

FINGERPRINT = entitlement.fingerprint("search", {"keywords": "data engineer"})
OTHER_FINGERPRINT = entitlement.fingerprint("search", {"keywords": "data analyst"})


@pytest.fixture()
def engine(tmp_path):
    # timeout raises SQLite's own lock wait above the 5s default: several threads contending
    # for the write lock on a slow machine would otherwise surface as "database is locked",
    # which is a property of the fixture and not of the code under test.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'entitlement.db'}", connect_args={"timeout": 30}
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def sessions(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def profile(sessions):
    db = sessions()
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    profile_id = profile.id
    db.close()
    return profile_id


def race(sessions, count, work):
    """Run `work(db, index)` in `count` threads released simultaneously.

    Returns a list of (result, exception) in thread order. Exceptions are captured rather than
    raised so a refusal is data the test can assert on -- half of these threads are *supposed*
    to be refused, and a refusal surfacing as a test error would hide the outcome being checked.
    """
    barrier = threading.Barrier(count)
    outcomes = [None] * count

    def run(index):
        db = sessions()
        try:
            barrier.wait(timeout=30)
            outcomes[index] = (work(db, index), None)
        except Exception as exc:  # noqa: BLE001 -- capturing the refusal is the point
            outcomes[index] = (None, exc)
        finally:
            db.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


def claim_with_own_key(db, index):
    return entitlement.claim(
        db,
        claim_with_own_key.profile_id,
        usage_type="search",
        idempotency_key=f"race-{index}-{uuid.uuid4().hex[:8]}",
        request_fingerprint=FINGERPRINT,
    )


class TestOnlyOneOfTwoSimultaneousRequestsGetsTheFreeSearch:
    @pytest.mark.parametrize("racers", [2, 5, 10])
    def test_exactly_one_claim_succeeds(self, sessions, profile, racers):
        """The requirement stated in the product rule, checked at three widths.

        Two is the case that is written down; five and ten are there because a check-then-act
        implementation can get lucky with two threads on a machine that happens to schedule
        them apart, and cannot get lucky ten times.
        """
        claim_with_own_key.profile_id = profile

        outcomes = race(sessions, racers, claim_with_own_key)

        granted = [row for row, exc in outcomes if row is not None]
        refused = [exc for _row, exc in outcomes if exc is not None]
        assert len(granted) == 1, f"{len(granted)} claims were granted, expected exactly 1"
        assert len(refused) == racers - 1
        assert all(isinstance(exc, entitlement.FreeLimitReachedError) for exc in refused)

    def test_the_database_holds_exactly_one_slot_afterwards(self, sessions, profile):
        """The assertion that survives any refactor of the Python: whatever the code did, the
        stored state must show one allowance taken."""
        claim_with_own_key.profile_id = profile

        race(sessions, 8, claim_with_own_key)

        db = sessions()
        holders = (
            db.query(JobHuntUsage)
            .filter(
                JobHuntUsage.profile_id == profile,
                JobHuntUsage.free_use_slot.isnot(None),
            )
            .all()
        )
        assert len(holders) == 1
        db.close()

    def test_two_different_students_racing_each_get_their_own(self, sessions, engine):
        """The constraint is scoped per profile, so contention between students is not a thing
        that exists. Worth pinning: a unique index on free_use_slot alone would have made the
        whole product single-user, and every single-profile test above would still pass."""
        db = sessions()
        profile_ids = []
        for _ in range(2):
            user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
            db.add(user)
            db.commit()
            student = StudentProfile(user_id=user.id)
            db.add(student)
            db.commit()
            profile_ids.append(student.id)
        db.close()

        def claim_for(session, index):
            return entitlement.claim(
                session,
                profile_ids[index],
                usage_type="search",
                idempotency_key=f"k-{uuid.uuid4().hex[:12]}",
                request_fingerprint=FINGERPRINT,
            )

        outcomes = race(sessions, 2, claim_for)

        assert all(exc is None for _row, exc in outcomes), [exc for _r, exc in outcomes]


class TestConcurrentRetriesOfTheSameOperation:
    def test_the_same_key_raced_produces_one_row_and_one_allowance(self, sessions, profile):
        """A retry storm -- the phone reissuing one tap several times as the connection
        flaps -- is one operation, however many requests it becomes."""

        def claim_same_key(db, _index):
            return entitlement.claim(
                db,
                profile,
                usage_type="search",
                idempotency_key="the-same-key-throughout",
                request_fingerprint=FINGERPRINT,
            )

        outcomes = race(sessions, 6, claim_same_key)

        assert all(exc is None for _row, exc in outcomes), [exc for _r, exc in outcomes]
        db = sessions()
        rows = db.query(JobHuntUsage).filter(JobHuntUsage.profile_id == profile).all()
        assert len(rows) == 1, "one operation must not become six ledger rows"
        assert rows[0].free_use_slot == entitlement.FREE_USE_SLOT
        db.close()

    def test_the_same_key_with_a_different_search_still_conflicts_under_a_race(
        self, sessions, profile
    ):
        """The fingerprint check is not a sequential-only nicety. If it were skipped whenever
        two requests overlapped, the bypass would be "send the second search fast enough"."""

        def claim_mixed(db, index):
            return entitlement.claim(
                db,
                profile,
                usage_type="search",
                idempotency_key="shared-key-two-searches",
                request_fingerprint=FINGERPRINT if index % 2 == 0 else OTHER_FINGERPRINT,
            )

        outcomes = race(sessions, 6, claim_mixed)

        refused = [exc for _row, exc in outcomes if exc is not None]
        assert refused, "at least one of the mismatched fingerprints must be refused"
        assert all(isinstance(exc, entitlement.IdempotencyConflictError) for exc in refused)

        db = sessions()
        assert db.query(JobHuntUsage).filter(JobHuntUsage.profile_id == profile).count() == 1
        db.close()


class TestSettleAndReleaseUnderContention:
    def test_settling_the_same_row_from_several_threads_is_harmless(self, sessions, profile):
        db = sessions()
        usage = entitlement.claim(
            db,
            profile,
            usage_type="search",
            idempotency_key="settle-race-key",
            request_fingerprint=FINGERPRINT,
        )
        usage_id = usage.id
        db.close()

        def settle(session, _index):
            row = session.get(JobHuntUsage, usage_id)
            entitlement.settle(session, row)
            return True

        outcomes = race(sessions, 5, settle)

        assert all(exc is None for _row, exc in outcomes), [exc for _r, exc in outcomes]
        db = sessions()
        row = db.get(JobHuntUsage, usage_id)
        assert row.status == "succeeded"
        assert row.free_use_slot == entitlement.FREE_USE_SLOT
        db.close()

    def test_a_release_racing_a_settle_cannot_refund_a_completed_search(
        self, sessions, profile
    ):
        """Both transitions are conditional on the row still being "claimed", so whichever
        lands first wins outright and the second writes nothing. The dangerous ordering is
        settle-then-release: if release were unconditional it would hand back a search that
        really did produce results."""
        db = sessions()
        usage = entitlement.claim(
            db,
            profile,
            usage_type="search",
            idempotency_key="settle-vs-release",
            request_fingerprint=FINGERPRINT,
        )
        usage_id = usage.id
        entitlement.settle(db, usage)
        db.close()

        def release(session, _index):
            entitlement.release(session, session.get(JobHuntUsage, usage_id))
            return True

        race(sessions, 4, release)

        db = sessions()
        row = db.get(JobHuntUsage, usage_id)
        assert row.status == "succeeded"
        assert row.free_use_slot == entitlement.FREE_USE_SLOT
        db.close()

    def test_a_released_claim_can_be_taken_again_by_a_racing_request(self, sessions, profile):
        """The other side of the refund: once handed back, the allowance is genuinely free for
        whichever request asks next -- and still only for one of them."""
        db = sessions()
        usage = entitlement.claim(
            db,
            profile,
            usage_type="search",
            idempotency_key="released-key",
            request_fingerprint=FINGERPRINT,
        )
        entitlement.release(db, usage)
        db.close()

        claim_with_own_key.profile_id = profile
        outcomes = race(sessions, 4, claim_with_own_key)

        granted = [row for row, exc in outcomes if row is not None]
        assert len(granted) == 1


class TestASubscriberIsNotRateLimitedByTheSlot:
    def test_many_simultaneous_premium_claims_all_succeed(self, sessions, profile):
        """The free slot must not accidentally serialise paying students: their rows carry no
        slot at all, so the unique constraint never applies to them."""
        db = sessions()
        user = db.get(StudentProfile, profile)
        grant_premium(db, db.get(User, user.user_id).email)
        db.close()

        claim_with_own_key.profile_id = profile
        outcomes = race(sessions, 8, claim_with_own_key)

        assert all(exc is None for _row, exc in outcomes), [exc for _r, exc in outcomes]
        db = sessions()
        rows = db.query(JobHuntUsage).filter(JobHuntUsage.profile_id == profile).all()
        assert len(rows) == 8
        assert all(row.free_use_slot is None and row.was_premium for row in rows)
        db.close()


class TestAnAbandonedClaimIsReclaimable:
    def test_a_claim_older_than_the_timeout_stops_holding_the_allowance(
        self, sessions, profile
    ):
        """Only reachable when a worker dies between claiming and settling. Without this, one
        crashed process would keep a student's single search reserved forever, with nothing
        they or support could do about it."""
        db = sessions()
        usage = entitlement.claim(
            db,
            profile,
            usage_type="search",
            idempotency_key="abandoned-key",
            request_fingerprint=FINGERPRINT,
        )
        # Backdated rather than waited for: the timeout is ten minutes, and a test that slept
        # would be measuring the clock instead of the behaviour.
        usage.created_at = datetime.datetime.utcnow() - entitlement._CLAIM_TIMEOUT
        usage.created_at -= datetime.timedelta(seconds=1)
        db.commit()
        db.close()

        db = sessions()
        fresh = entitlement.claim(
            db,
            profile,
            usage_type="search",
            idempotency_key="after-the-crash",
            request_fingerprint=FINGERPRINT,
        )
        assert fresh.free_use_slot == entitlement.FREE_USE_SLOT
        stale = db.query(JobHuntUsage).filter_by(idempotency_key="abandoned-key").one()
        assert stale.status == "abandoned"
        assert stale.free_use_slot is None
        db.close()

    def test_a_recent_claim_is_not_reclaimed(self, sessions, profile):
        """The guard against the reclaim becoming its own bypass: a claim that is merely
        in flight -- a slow provider, a search still running -- must keep the allowance."""
        db = sessions()
        entitlement.claim(
            db,
            profile,
            usage_type="search",
            idempotency_key="still-running",
            request_fingerprint=FINGERPRINT,
        )
        db.close()

        db = sessions()
        with pytest.raises(entitlement.FreeLimitReachedError):
            entitlement.claim(
                db,
                profile,
                usage_type="search",
                idempotency_key="the-impatient-second-tap",
                request_fingerprint=FINGERPRINT,
            )
        db.close()

    def test_several_requests_racing_one_stale_claim_still_yield_one_winner(
        self, sessions, profile
    ):
        """Reclaiming is itself concurrent, and must not become a moment where two requests
        both believe they freed the slot and both take it."""
        db = sessions()
        usage = entitlement.claim(
            db,
            profile,
            usage_type="search",
            idempotency_key="stale-under-contention",
            request_fingerprint=FINGERPRINT,
        )
        usage.created_at = (
            datetime.datetime.utcnow() - entitlement._CLAIM_TIMEOUT - datetime.timedelta(minutes=1)
        )
        db.commit()
        db.close()

        claim_with_own_key.profile_id = profile
        outcomes = race(sessions, 6, claim_with_own_key)

        granted = [row for row, exc in outcomes if row is not None]
        assert len(granted) == 1, f"{len(granted)} requests reclaimed the same stale slot"
