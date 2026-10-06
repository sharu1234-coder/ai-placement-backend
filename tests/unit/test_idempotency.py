"""The replay-safety mechanism itself: keys, fingerprints, claims, and who wins a race.

A file-backed SQLite database rather than conftest's shared in-memory one, for the reason
tests/unit/test_job_hunt_concurrency.py gives at length: that fixture uses StaticPool, so every
session shares one connection and one transaction, and two "concurrent" sessions would commit
each other's pending writes. A concurrency test over StaticPool passes no matter what the code
does, which is worse than having no test.

These also run against PostgreSQL when TEST_DATABASE_URL is set. That matters here specifically
because a failed statement poisons a PostgreSQL transaction until it is rolled back, so the
savepoint-and-rollback recovery in idempotency._insert can look correct on SQLite and still be
wrong in production.
"""

import datetime
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.database.base import Base
from app.models.operation_claim import OperationClaim
from app.models.profile import StudentProfile
from app.models.user import User
from app.services import idempotency

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

OPERATION = "assessment.start"


@pytest.fixture()
def engine(tmp_path):
    if _TEST_DATABASE_URL:
        eng = create_engine(_TEST_DATABASE_URL, poolclass=NullPool,
                            connect_args={"options": "-c lock_timeout=15000"})
        Base.metadata.drop_all(bind=eng)
    else:
        # A file, not :memory:, so a second session in a second thread is genuinely a second
        # connection. timeout lets a writer wait for SQLite's lock rather than failing instantly.
        eng = create_engine(f"sqlite:///{tmp_path / 'idem.db'}",
                            connect_args={"timeout": 30})
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        if _TEST_DATABASE_URL:
            Base.metadata.drop_all(bind=eng)
        eng.dispose()


@pytest.fixture()
def sessions(engine):
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    made = []

    def make():
        s = factory()
        made.append(s)
        return s

    try:
        yield make
    finally:
        for s in made:
            s.close()


@pytest.fixture()
def db(sessions):
    return sessions()


def make_profile(db, email=None) -> StudentProfile:
    user = User(email=email or f"p{os.urandom(4).hex()}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


# ==========================================================================================
# 1. Keys
# ==========================================================================================

class TestKeys:
    def test_a_usable_key_is_accepted_unchanged(self):
        assert idempotency.normalise_key("and-0123456789abcdef") == "and-0123456789abcdef"

    def test_a_missing_key_is_not_an_error_and_cannot_be_replayed_against(self):
        # The safe default: no key means this request is its own operation. A client that
        # forgets to send one behaves exactly as it did before the mechanism existed.
        first = idempotency.normalise_key(None)
        second = idempotency.normalise_key("   ")
        assert first.startswith("srv-") and second.startswith("srv-")
        assert first != second

    @pytest.mark.parametrize("raw", ["short", "a" * 65, "has space", "has/slash", "emoji✨"])
    def test_an_unusable_key_is_refused(self, raw):
        with pytest.raises(idempotency.InvalidIdempotencyKeyError):
            idempotency.normalise_key(raw)

    @pytest.mark.parametrize("raw", ["a" * 8, "a" * 64, "A.b_c:d-1"])
    def test_the_documented_boundaries_are_inclusive(self, raw):
        assert idempotency.normalise_key(raw) == raw

    def test_surrounding_whitespace_does_not_make_two_keys(self):
        assert idempotency.normalise_key("  key-12345678  ") == "key-12345678"


# ==========================================================================================
# 2. Fingerprints
# ==========================================================================================

class TestFingerprints:
    def test_the_same_request_hashes_the_same_whatever_the_dict_order(self):
        a = idempotency.fingerprint(OPERATION, {"blueprint_id": 7, "role": "SDE"})
        b = idempotency.fingerprint(OPERATION, {"role": "SDE", "blueprint_id": 7})
        assert a == b and len(a) == 64

    def test_a_different_value_is_a_different_request(self):
        assert idempotency.fingerprint(OPERATION, {"blueprint_id": 7}) != \
            idempotency.fingerprint(OPERATION, {"blueprint_id": 8})

    def test_a_value_s_type_is_part_of_its_identity(self):
        # "5" and 5 are not the same request and must not be allowed to collide.
        assert idempotency.fingerprint(OPERATION, {"blueprint_id": 5}) != \
            idempotency.fingerprint(OPERATION, {"blueprint_id": "5"})

    def test_the_operation_is_part_of_the_material(self):
        # So one key cannot be reused across two endpoints and be handed the other's result.
        assert idempotency.fingerprint("a.start", {"x": 1}) != \
            idempotency.fingerprint("b.start", {"x": 1})

    def test_absent_and_null_parameters_agree(self):
        assert idempotency.fingerprint(OPERATION, {"x": 1, "y": None}) == \
            idempotency.fingerprint(OPERATION, {"x": 1})

    def test_an_upload_is_fingerprinted_by_a_digest_of_its_bytes(self):
        # Bounded and deterministic, and the point is what is *not* here: the file never
        # reaches the table.
        digest = idempotency.body_digest(b"%PDF-1.4 ...")
        assert len(digest) == 64
        assert digest == idempotency.body_digest(b"%PDF-1.4 ...")
        assert digest != idempotency.body_digest(b"%PDF-1.4 .. different")


# ==========================================================================================
# 3. Claim, replay, conflict
# ==========================================================================================

class TestClaimLifecycle:
    def test_a_first_claim_is_fresh_and_owns_the_operation(self, db):
        profile = make_profile(db)
        outcome = idempotency.claim(db, profile.id, operation=OPERATION,
                                   idempotency_key="key-00000001", params={"blueprint_id": 7})
        assert not outcome.is_replay
        assert outcome.status == "claimed"

    def test_a_replay_after_success_returns_the_original_result(self, db):
        profile = make_profile(db)
        first = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=101)

        again = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})

        # This is the whole point of the phase: the second arrival is told what the first did.
        assert again.is_replay
        assert (again.result_kind, again.result_id) == ("assessment", 101)
        assert db.query(OperationClaim).count() == 1

    def test_the_same_key_carrying_a_different_request_is_a_conflict(self, db):
        profile = make_profile(db)
        first = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=101)

        with pytest.raises(idempotency.IdempotencyConflictError):
            idempotency.claim(db, profile.id, operation=OPERATION,
                              idempotency_key="key-00000001", params={"blueprint_id": 999})

    def test_a_replay_while_the_first_is_still_running_does_not_start_a_second(self, db):
        profile = make_profile(db)
        idempotency.claim(db, profile.id, operation=OPERATION,
                          idempotency_key="key-00000001", params={"blueprint_id": 7})

        # Refusing is the point. For the AI-backed routes the first attempt is already paying
        # for the model call, and a second would pay for it again.
        with pytest.raises(idempotency.OperationInProgressError):
            idempotency.claim(db, profile.id, operation=OPERATION,
                              idempotency_key="key-00000001", params={"blueprint_id": 7})

    def test_an_explicit_retry_after_a_genuine_failure_proceeds(self, db):
        profile = make_profile(db)
        first = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        idempotency.release(db, first.claim_id)

        retry = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})

        # Nothing durable came of the first attempt, so the student must not be stranded.
        assert not retry.is_replay
        assert retry.status == "claimed"
        assert db.query(OperationClaim).count() == 1, "one row per logical operation"

    def test_a_release_cannot_undo_a_success(self, db):
        # A replayed key whose second run happened to fail must not erase the first success.
        profile = make_profile(db)
        first = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=101)
        idempotency.release(db, first.claim_id)

        row = db.query(OperationClaim).one()
        assert row.status == "succeeded" and row.result_id == 101

    def test_settling_twice_writes_nothing_the_second_time(self, db):
        profile = make_profile(db)
        first = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=101)
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=222)

        assert db.query(OperationClaim).one().result_id == 101

    def test_a_claim_whose_worker_died_is_reclaimable(self, db):
        profile = make_profile(db)
        first = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        # Only reachable when a worker is killed between claiming and settling. Without this a
        # student would be locked out of their own operation until the row aged out.
        stale = idempotency._utcnow() - idempotency.CLAIM_TIMEOUT - datetime.timedelta(minutes=1)
        db.query(OperationClaim).filter(OperationClaim.id == first.claim_id).update(
            {"created_at": stale}
        )
        db.commit()

        retry = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        assert not retry.is_replay

    def test_a_key_older_than_the_replay_window_is_not_a_handle_on_the_old_result(self, db):
        profile = make_profile(db)
        first = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=101)
        old = idempotency._utcnow() - idempotency.REPLAY_WINDOW - datetime.timedelta(minutes=1)
        db.query(OperationClaim).filter(OperationClaim.id == first.claim_id).update(
            {"completed_at": old, "created_at": old}
        )
        db.commit()

        with pytest.raises(idempotency.IdempotencyConflictError):
            idempotency.claim(db, profile.id, operation=OPERATION,
                              idempotency_key="key-00000001", params={"blueprint_id": 7})


# ==========================================================================================
# 4. Ownership
# ==========================================================================================

class TestReplayDoesNotWrite:
    """A replay must resolve with reads only.

    This pins a design decision rather than a user-visible behaviour, which is why it needs its
    own test: the first version of `claim` ran an UPDATE and a DELETE *before* the lookup, so
    every arrival -- including a pure replay -- opened a write transaction. Five concurrent
    duplicates then contended for SQLite's single write lock and two came back "database is
    locked". The invariant still held, but a retry storm is exactly when this path must not be
    fighting itself.

    Without this test the mutation that removes the pre-read survives, because `_insert`'s
    IntegrityError recovery quietly produces the right answer anyway -- correct, and one wasted
    write per replay.
    """

    @staticmethod
    def _count_writes(engine, during):
        from sqlalchemy import event

        statements = []

        def before_cursor_execute(conn, cursor, statement, params, context, executemany):
            head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
            if head in ("INSERT", "UPDATE", "DELETE"):
                statements.append(head)

        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            during()
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)
        return statements

    def test_a_replay_of_a_succeeded_operation_writes_nothing(self, db, engine):
        profile = make_profile(db)
        first = idempotency.claim(db, profile.id, operation=OPERATION,
                                 idempotency_key="key-00000001", params={"blueprint_id": 7})
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=101)

        outcome = {}

        def replay():
            outcome["result"] = idempotency.claim(
                db, profile.id, operation=OPERATION,
                idempotency_key="key-00000001", params={"blueprint_id": 7},
            )

        writes = self._count_writes(engine, replay)

        assert outcome["result"].is_replay
        assert writes == [], f"a replay should read only, but issued {writes}"

    def test_a_replay_of_an_in_flight_operation_writes_nothing(self, db, engine):
        profile = make_profile(db)
        idempotency.claim(db, profile.id, operation=OPERATION,
                          idempotency_key="key-00000001", params={"blueprint_id": 7})

        def replay():
            with pytest.raises(idempotency.OperationInProgressError):
                idempotency.claim(db, profile.id, operation=OPERATION,
                                  idempotency_key="key-00000001", params={"blueprint_id": 7})

        writes = self._count_writes(engine, replay)
        assert writes == [], f"refusing a duplicate should read only, but issued {writes}"


class TestOwnership:
    def test_two_students_may_use_the_same_key_independently(self, db):
        a, b = make_profile(db), make_profile(db)
        first = idempotency.claim(db, a.id, operation=OPERATION,
                                 idempotency_key="shared-key-1", params={"blueprint_id": 7})
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=101)

        other = idempotency.claim(db, b.id, operation=OPERATION,
                                 idempotency_key="shared-key-1", params={"blueprint_id": 7})

        # Not a replay, and emphatically not profile A's result.
        assert not other.is_replay
        assert other.result_id is None
        assert db.query(OperationClaim).count() == 2

    def test_one_student_cannot_reach_another_s_stored_result(self, db):
        a, b = make_profile(db), make_profile(db)
        first = idempotency.claim(db, a.id, operation=OPERATION,
                                 idempotency_key="shared-key-1", params={"blueprint_id": 7})
        idempotency.settle(db, first.claim_id, result_kind="assessment", result_id=101)

        outcome = idempotency.claim(db, b.id, operation=OPERATION,
                                   idempotency_key="shared-key-1", params={"blueprint_id": 7})
        assert (outcome.result_kind, outcome.result_id) == (None, None)

    def test_different_keys_are_different_operations(self, db):
        profile = make_profile(db)
        for key in ("key-00000001", "key-00000002"):
            outcome = idempotency.claim(db, profile.id, operation=OPERATION,
                                       idempotency_key=key, params={"blueprint_id": 7})
            idempotency.settle(db, outcome.claim_id, result_kind="assessment", result_id=1)
        assert db.query(OperationClaim).count() == 2


# ==========================================================================================
# 5. Retention
# ==========================================================================================

class TestRetention:
    def test_long_settled_rows_are_swept_on_the_next_claim(self, db):
        profile = make_profile(db)
        old = idempotency.claim(db, profile.id, operation=OPERATION,
                               idempotency_key="key-00000001", params={"blueprint_id": 7})
        idempotency.settle(db, old.claim_id, result_kind="assessment", result_id=101)
        past = idempotency._utcnow() - idempotency.RETENTION - datetime.timedelta(days=1)
        db.query(OperationClaim).filter(OperationClaim.id == old.claim_id).update(
            {"created_at": past}
        )
        db.commit()

        idempotency.claim(db, profile.id, operation=OPERATION,
                          idempotency_key="key-00000002", params={"blueprint_id": 7})

        remaining = db.query(OperationClaim).all()
        assert [r.idempotency_key for r in remaining] == ["key-00000002"]

    def test_a_claim_still_in_flight_is_never_swept(self, db):
        # The first version of this test backdated the in-flight claim past RETENTION and
        # expected it to survive, which was simply wrong: a claim eight days old is not in
        # flight, it is a worker that died, and removing it is correct. The property worth
        # pinning is the one a retry storm depends on -- a claim that is genuinely running is
        # not swept out from under itself by a concurrent request's housekeeping.
        profile = make_profile(db)
        running = idempotency.claim(db, profile.id, operation=OPERATION,
                                   idempotency_key="key-00000001", params={"blueprint_id": 7})
        stale_settled = idempotency.claim(db, profile.id, operation=OPERATION,
                                         idempotency_key="key-00000002", params={"x": 1})
        idempotency.settle(db, stale_settled.claim_id, result_kind="assessment", result_id=9)
        past = idempotency._utcnow() - idempotency.RETENTION - datetime.timedelta(days=1)
        db.query(OperationClaim).filter(OperationClaim.id == stale_settled.claim_id).update(
            {"created_at": past}
        )
        db.commit()

        idempotency.claim(db, profile.id, operation=OPERATION,
                          idempotency_key="key-00000003", params={"x": 2})

        keys = sorted(r.idempotency_key for r in db.query(OperationClaim).all())
        assert keys == ["key-00000001", "key-00000003"], \
            "the running claim survived; only the long-settled row went"
        assert db.get(OperationClaim, running.claim_id).status == "claimed"


# ==========================================================================================
# 6. Concurrency -- the database arbitrates, not Python
# ==========================================================================================

class TestConcurrency:
    @staticmethod
    def _race(sessions, profile_id, *, count, key, params):
        """`count` genuine threads, each with its own session, all claiming at once."""
        start = threading.Barrier(count)
        results, errors = [], []

        def attempt():
            session = sessions()
            start.wait(timeout=30)
            try:
                results.append(idempotency.claim(
                    session, profile_id, operation=OPERATION,
                    idempotency_key=key, params=params,
                ))
            except Exception as exc:  # noqa: BLE001 -- the classification is the assertion
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(lambda _: attempt(), range(count)))
        return results, errors

    def test_two_concurrent_duplicates_produce_one_claim(self, db, sessions):
        profile = make_profile(db)

        results, errors = self._race(sessions, profile.id, count=2,
                                     key="key-00000001", params={"blueprint_id": 7})

        assert db.query(OperationClaim).count() == 1, "the unique constraint arbitrated"
        fresh = [r for r in results if not r.is_replay]
        assert len(fresh) == 1, f"exactly one winner (fresh={len(fresh)}, errors={errors})"
        # Every loser was told the operation is taken, not handed a second one.
        assert all(isinstance(e, idempotency.OperationInProgressError) for e in errors), errors

    def test_five_concurrent_duplicates_produce_one_claim(self, db, sessions):
        profile = make_profile(db)

        results, errors = self._race(sessions, profile.id, count=5,
                                     key="key-00000001", params={"blueprint_id": 7})

        assert db.query(OperationClaim).count() == 1
        assert len([r for r in results if not r.is_replay]) == 1, \
            f"one winner of five (errors={errors})"
        assert all(isinstance(e, idempotency.OperationInProgressError) for e in errors), errors

    def test_concurrent_requests_with_different_keys_are_both_legitimate(self, db, sessions):
        profile = make_profile(db)
        done = []

        def attempt(key):
            session = sessions()
            done.append(idempotency.claim(session, profile.id, operation=OPERATION,
                                          idempotency_key=key, params={"blueprint_id": 7}))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(attempt, ["key-00000001", "key-00000002"]))

        assert db.query(OperationClaim).count() == 2
        assert all(not o.is_replay for o in done)

    def test_concurrent_duplicates_with_a_different_fingerprint_still_conflict(self, db, sessions):
        # "Send the second one fast enough" must not be a way around the fingerprint check.
        profile = make_profile(db)
        start = threading.Barrier(2)
        outcomes, errors = [], []

        def attempt(blueprint_id):
            session = sessions()
            start.wait(timeout=30)
            try:
                outcomes.append(idempotency.claim(
                    session, profile.id, operation=OPERATION,
                    idempotency_key="key-00000001", params={"blueprint_id": blueprint_id},
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(attempt, [7, 999]))

        assert db.query(OperationClaim).count() == 1
        assert len(outcomes) == 1
        assert len(errors) == 1 and isinstance(
            errors[0], (idempotency.IdempotencyConflictError,
                        idempotency.OperationInProgressError)
        ), errors

    def test_losing_the_race_leaves_the_session_usable(self, db, sessions):
        """The property a sequential test cannot show.

        The savepoint undoes the failed INSERT, but the loser's read snapshot predates the
        winner's commit, so the rollback in _insert is what lets it see the row that beat it.
        Without it the loser re-reads "no such row", tries again, and fails again -- and on
        PostgreSQL every later statement in that transaction raises instead.
        """
        profile = make_profile(db)
        loser = sessions()
        winner = sessions()

        idempotency.claim(winner, profile.id, operation=OPERATION,
                          idempotency_key="key-00000001", params={"blueprint_id": 7})

        with pytest.raises(idempotency.OperationInProgressError):
            idempotency.claim(loser, profile.id, operation=OPERATION,
                              idempotency_key="key-00000001", params={"blueprint_id": 7})

        # The session still works afterwards, which is what the rollback buys.
        assert loser.query(OperationClaim).count() == 1
