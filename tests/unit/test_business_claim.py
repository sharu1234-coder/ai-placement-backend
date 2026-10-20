"""The business-claim layer: one generation per resource, and no way to get stuck.

tests/api/test_preparation_plan_concurrency.py races the endpoint, which is where the defect
actually arrives. This file is for the decisions underneath it that a race cannot reach on
purpose -- a worker that died mid-generation, a resource that was deleted after it was built, a
waiter that runs out of patience, a client trying to send the server's own derived key.

A file-backed SQLite database rather than conftest's shared in-memory one, for the reason
tests/unit/test_idempotency.py gives at length: that fixture uses StaticPool, so every session
shares one connection and one transaction, and two "concurrent" sessions would commit each
other's pending writes. These also run against PostgreSQL when TEST_DATABASE_URL is set.
"""

import datetime
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.database.base import Base
from app.models.operation_claim import OperationClaim
from app.models.profile import StudentProfile
from app.models.user import User
from app.services import idempotency
from tests import db_safety

# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()

OPERATION = "jd.preparation_plan"
IDENTITY = {"jd_id": 7}


@pytest.fixture()
def engine(tmp_path):
    """The database these concurrency tests actually run against -- and what that is worth.

    **On SQLite (the default here, and what every result in this file was produced on.)** A file
    rather than `:memory:`, so a second session on a second thread is a genuinely separate
    connection, and `timeout=30` lets a writer wait for the database lock instead of failing
    instantly. That is enough to exercise the real code paths: two connections racing the same
    `UNIQUE (profile_id, idempotency_key)` insert, the loser's `IntegrityError`, the savepoint
    rollback, the re-read, and the polling wait. The deterministic coalescing proof above holds
    here.

    What SQLite cannot establish is how the race is *arbitrated*. SQLite serialises writers with
    a single database-level lock, so the loser of the insert race has typically been made to wait
    for the winner's whole transaction. PostgreSQL arbitrates per row: a concurrent insert of the
    same key blocks on the uncommitted tuple and then raises on the winner's commit, and the
    re-read after `db.rollback()` only sees the winner's row because READ COMMITTED takes a fresh
    snapshot per statement -- under REPEATABLE READ it would not. The production code is written
    for that model (see `_insert`, which rolls back explicitly and says why), but *written for*
    is not *tested on*.

    **So: PostgreSQL concurrency is NOT PROVEN by this file.** Not "probably fine" -- untested.
    Running it there needs one environment variable and nothing else: set `TEST_DATABASE_URL` and
    this fixture uses PostgreSQL with a 15 s `lock_timeout`. As of 2026-10-14 a server is
    listening on this host's port 5432, but no `TEST_DATABASE_URL` is set and credentials are
    never probed for, which is the same reason
    `tests/scripts/test_backup_restore_roundtrip.py` skips.
    """
    if _TEST_DATABASE_URL:
        eng = create_engine(_TEST_DATABASE_URL, poolclass=NullPool,
                            connect_args={"options": "-c lock_timeout=15000"})
        db_safety.drop_all_safely(eng)
    else:
        # A file, not :memory:, so a second session in a second thread is genuinely a second
        # connection. timeout lets a writer wait for SQLite's lock rather than failing instantly.
        eng = create_engine(f"sqlite:///{tmp_path / 'business.db'}", connect_args={"timeout": 30})
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        if _TEST_DATABASE_URL:
            db_safety.drop_all_safely(eng)
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


def make_profile(db) -> StudentProfile:
    user = User(email=f"p{os.urandom(4).hex()}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


class Resource:
    """Stands in for the durable thing a generation produces.

    Deliberately not the plan table. What is under test is the layer's own decision-making, and a
    fake resource makes "the resource exists", "it was deleted", and "the generation failed" three
    settings rather than three fixtures.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.generations = 0
        self.value = None
        self.fail = False
        self.gate: threading.Event = None

    def perform(self):
        with self.lock:
            self.generations += 1
            number = self.generations
        if self.gate is not None:
            assert self.gate.wait(timeout=60), "the generation gate was never released"
        if self.fail:
            raise RuntimeError("generation failed")
        self.value = f"result #{number}"
        return self.value, number

    def read_back(self, result_id=None):
        """Looked up by identity, ignoring the result id.

        Which is one of the two legitimate choices the layer allows -- a resource that exists for
        good outlives the claim that recorded it. [TestRegenerate] uses the other.
        """
        return self.value


def run(db, profile_id, resource, **kwargs):
    return idempotency.run_business_once(
        db,
        profile_id,
        operation=OPERATION,
        identity=IDENTITY,
        perform=resource.perform,
        read_back=resource.read_back,
        result_kind="job_preparation_plan",
        **kwargs,
    )


class Repeatable(Resource):
    """A resource whose generation is itself the operation, like a placement path.

    Keeps every generation rather than overwriting one value, and reads back *by result id* --
    which is what a waiter is owed: the row produced by the generation it watched, not whatever a
    later regeneration has since put in its place.
    """

    def __init__(self):
        super().__init__()
        self.produced = {}

    def perform(self):
        value, number = super().perform()
        self.produced[number] = value
        return value, number

    def read_back(self, result_id=None):
        return self.produced.get(result_id)


def regenerate(db, profile_id, resource, **kwargs):
    return idempotency.run_business_once(
        db,
        profile_id,
        operation="placement_path.generate",
        identity={"profile_id": profile_id},
        perform=resource.perform,
        read_back=resource.read_back,
        result_kind="placement_path",
        on_settled=idempotency.REGENERATE,
        **kwargs,
    )


def await_generation(resource, timeout=60):
    """Block until a generation has started, so the race is observed rather than hoped for."""
    limit = time.monotonic() + timeout
    while resource.generations == 0:
        assert time.monotonic() < limit, "no generation ever started"
        time.sleep(0.01)


class WaiterWatch:
    """Witnesses the moment a request registers as waiting on an in-flight claim.

    ## Why this exists

    These tests used to release the owner after `time.sleep(0.4)`, with a comment saying it was
    "long enough for the waiter to have found the claim taken and entered the wait". It was not
    always long enough. `test_concurrent_requests_generate_once_and_a_later_one_generates_again`
    failed once in a full suite run and in 1 of 15 isolated runs, reporting "three overlapping
    requests generated 2 times" -- and both numbers were *correct answers to different
    questions*. A follower that reaches the claim check after the owner has settled is not a
    concurrent duplicate; for `on_settled=REGENERATE` it is a new deliberate request, and
    generating again is the documented policy, pinned separately by
    `test_a_request_arriving_after_settlement_regenerates`. The old test could not tell the two
    situations apart, so it asserted the coalescing property while sometimes exercising the
    regeneration one.

    ## What is intercepted, and why this is not a mock of the contract

    `run_business_once` calls `time.sleep` from exactly one place: the branch it takes after
    reading the claim as `claimed` and not stale, immediately before setting its own `waited`
    flag. Entering that sleep is therefore not a scheduling artefact to be guessed at -- it *is*
    the production act these tests need to witness, the point at which a request has joined the
    owner's generation and so is owed the owner's result.

    Nothing about the concurrency contract is replaced or simulated. The module's clock is
    swapped for one that reports each thread's first arrival in the wait and then sleeps for
    real, merely briefly, so the test is not paced by the production poll interval. Swapping the
    module's `time` rather than the global one keeps the interception inside the code under test.
    """

    def __init__(self, monkeypatch, cap=0.01):
        self._joined = threading.Semaphore(0)
        self._seen = set()
        self._lock = threading.Lock()
        real_sleep, real_monotonic = time.sleep, time.monotonic

        def observed_sleep(seconds):
            thread = threading.get_ident()
            with self._lock:
                first = thread not in self._seen
                self._seen.add(thread)
            if first:
                self._joined.release()
            real_sleep(min(seconds, cap))

        monkeypatch.setattr(
            idempotency,
            "time",
            SimpleNamespace(sleep=observed_sleep, monotonic=real_monotonic),
        )

    def await_joined(self, count, timeout=60):
        """Block until `count` distinct requests have joined the in-flight claim.

        Fails rather than proceeding if they never do. A test that released the owner anyway
        would be back to asserting coalescing while exercising something else.
        """
        for index in range(count):
            assert self._joined.acquire(timeout=timeout), (
                "only %d of %d requests registered as waiting on the in-flight claim, so this "
                "test is not exercising the coalescing branch at all" % (index, count)
            )

    @property
    def joined(self):
        with self._lock:
            return len(self._seen)


def claims(db, profile_id):
    return (
        db.query(OperationClaim)
        .filter(OperationClaim.profile_id == profile_id)
        .order_by(OperationClaim.id)
        .all()
    )


# ==========================================================================================
# 1. The derived key
# ==========================================================================================


class TestDerivedKey:
    def test_the_same_identity_always_derives_the_same_key(self):
        assert idempotency.business_key(OPERATION, {"jd_id": 7}) == idempotency.business_key(
            OPERATION, {"jd_id": 7}
        )

    def test_a_different_identity_derives_a_different_key(self):
        assert idempotency.business_key(OPERATION, {"jd_id": 7}) != idempotency.business_key(
            OPERATION, {"jd_id": 8}
        )

    def test_a_different_operation_derives_a_different_key(self):
        assert idempotency.business_key("a.operation", IDENTITY) != idempotency.business_key(
            "b.operation", IDENTITY
        )

    def test_the_key_order_does_not_depend_on_dict_order(self):
        assert idempotency.business_key(OPERATION, {"a": 1, "b": 2}) == idempotency.business_key(
            OPERATION, {"b": 2, "a": 1}
        )

    def test_a_client_can_never_send_a_derived_key(self):
        """The whole of the no-hijacking guarantee, and it is worth pinning rather than trusting.

        If `_KEY_PATTERN` ever gained '@', a client could take the business claim for a resource
        and every later request would wait on a generation nobody was running -- a denial of
        service on one job description, available to anyone who read this file.
        """
        derived = idempotency.business_key(OPERATION, IDENTITY)
        with pytest.raises(idempotency.InvalidIdempotencyKeyError):
            idempotency.normalise_key(derived)

    def test_a_long_identity_is_hashed_rather_than_truncated(self):
        """Truncation would be a silent collision: two long identities sharing a 64-character
        prefix would become one business operation and one of them would never be generated."""
        first = idempotency.business_key(OPERATION, {"a": "x" * 80, "b": 1})
        second = idempotency.business_key(OPERATION, {"a": "x" * 80, "b": 2})

        assert len(first) <= 64 and len(second) <= 64
        assert first != second
        assert first.startswith("biz@")
        with pytest.raises(idempotency.InvalidIdempotencyKeyError):
            idempotency.normalise_key(first)


# ==========================================================================================
# 2. One generation per resource
# ==========================================================================================


class TestOneGeneration:
    def test_the_first_request_generates(self, db):
        profile = make_profile(db)
        resource = Resource()

        assert run(db, profile.id, resource) == "result #1"
        assert resource.generations == 1

    def test_a_later_request_is_answered_from_the_resource(self, db):
        profile = make_profile(db)
        resource = Resource()
        run(db, profile.id, resource)

        assert run(db, profile.id, resource) == "result #1"
        assert resource.generations == 1, "the resource existed; nothing should have run"

    def test_the_claim_is_reused_rather_than_accumulated(self, db):
        profile = make_profile(db)
        resource = Resource()
        run(db, profile.id, resource)
        run(db, profile.id, resource)

        stored = claims(db, profile.id)
        assert [c.status for c in stored] == ["succeeded"]
        assert stored[0].idempotency_key.startswith("biz@")

    def test_two_profiles_derive_the_same_key_and_still_get_their_own_generation(self, db):
        """`UNIQUE (profile_id, idempotency_key)` is what makes a derived key safe to share.

        The key says nothing about who owns it, so without the profile in that constraint the
        second student would have been handed the first student's result.
        """
        first = make_profile(db)
        second = make_profile(db)
        first_resource, second_resource = Resource(), Resource()

        run(db, first.id, first_resource)
        run(db, second.id, second_resource)

        assert (first_resource.generations, second_resource.generations) == (1, 1)
        assert idempotency.business_key(OPERATION, IDENTITY) == claims(db, first.id)[0].idempotency_key
        assert len(claims(db, first.id)) == 1, claims(db, first.id)
        assert len(claims(db, second.id)) == 1, (
            "each student must own their own claim row. A lookup that ignored profile_id would "
            "leave this student with none, and their request answered from the other's claim."
        )
        assert claims(db, first.id)[0].id != claims(db, second.id)[0].id

    def test_concurrent_requests_generate_once(self, sessions):
        """The race, at this layer, with a session per thread."""
        setup = sessions()
        profile = make_profile(setup)
        resource = Resource()
        resource.gate = threading.Event()
        started = threading.Barrier(3, timeout=60)

        def attempt():
            own = sessions()
            started.wait()
            return run(own, profile.id, resource)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(attempt) for _ in range(3)]
            # Let the owner get as far as generating before releasing it, so the other two are
            # certain to have found a claim already taken.
            await_generation(resource)
            resource.gate.set()
            results = [f.result(timeout=60) for f in futures]

        assert resource.generations == 1, f"three requests generated {resource.generations} times"
        assert results == ["result #1"] * 3


# ==========================================================================================
# 3. Nothing can get permanently stuck
# ==========================================================================================


class TestRecovery:
    def test_a_failed_generation_leaves_the_resource_generatable(self, db):
        profile = make_profile(db)
        resource = Resource()
        resource.fail = True
        with pytest.raises(RuntimeError):
            run(db, profile.id, resource)

        assert claims(db, profile.id)[0].status == "failed"

        resource.fail = False
        assert run(db, profile.id, resource) == "result #2"
        assert claims(db, profile.id)[0].status == "succeeded"

    def test_a_claim_whose_worker_died_is_recovered_rather_than_waited_on(self, db, monkeypatch):
        """A crash between claiming and settling must not strand the student.

        Backdated past CLAIM_TIMEOUT, which is the only way to reach this without killing a
        process. The 15-minute default is deliberately far longer than the longest generation this
        app can run -- the production provider is capped at 120s -- so recovery can never start a
        second generation while the first is still legitimately working.
        """
        # Shortened so that "recovered" and "waited out the full patience bound" cannot be
        # confused: recovery is supposed to be immediate, so a run that takes the deadline is a
        # failure even though it ends with the same value.
        monkeypatch.setattr(idempotency, "BUSINESS_WAIT", datetime.timedelta(seconds=0.5))
        monkeypatch.setattr(idempotency, "BUSINESS_POLL_FIRST", 0.05)
        profile = make_profile(db)
        resource = Resource()
        stale = OperationClaim(
            profile_id=profile.id,
            operation=OPERATION,
            idempotency_key=idempotency.business_key(OPERATION, IDENTITY),
            request_fingerprint=idempotency.fingerprint(OPERATION, IDENTITY),
            status="claimed",
            created_at=datetime.datetime.utcnow() - idempotency.CLAIM_TIMEOUT
            - datetime.timedelta(minutes=1),
        )
        db.add(stale)
        db.commit()

        assert run(db, profile.id, resource) == "result #1"
        assert resource.generations == 1
        assert claims(db, profile.id)[0].status == "succeeded"

    def test_the_stale_timeout_cannot_overlap_two_generations(self):
        """The constraint section 15 asks to be chosen rather than guessed.

        A stale timeout shorter than the longest possible generation would let a second request
        reclaim a claim whose owner is still calling the model, which is the duplication this
        phase removes, reintroduced by a constant.
        """
        from app.core.config import settings

        longest_generation = max(settings.ollama_timeout_seconds, settings.anthropic_timeout_seconds)
        assert idempotency.CLAIM_TIMEOUT.total_seconds() > longest_generation

    def test_a_waiter_gives_up_in_bounded_time_rather_than_hanging(self, db, monkeypatch):
        """The patience bound, measured. Exceeding it is an answer, not a hang.

        Monkeypatched down from 60s because the real value is chosen for a student on a phone, not
        for a test suite -- what is under test is that the deadline is honoured at all.
        """
        monkeypatch.setattr(idempotency, "BUSINESS_WAIT", datetime.timedelta(seconds=0.5))
        monkeypatch.setattr(idempotency, "BUSINESS_POLL_FIRST", 0.05)
        profile = make_profile(db)
        running = OperationClaim(
            profile_id=profile.id,
            operation=OPERATION,
            idempotency_key=idempotency.business_key(OPERATION, IDENTITY),
            request_fingerprint=idempotency.fingerprint(OPERATION, IDENTITY),
            status="claimed",
            created_at=datetime.datetime.utcnow(),
        )
        db.add(running)
        db.commit()
        resource = Resource()

        with pytest.raises(idempotency.OperationInProgressError):
            run(db, profile.id, resource)

        assert resource.generations == 0, "a waiter must never start a second generation"

    def test_a_waiter_blocked_on_a_running_generation_is_answered_with_its_result(
        self, sessions, monkeypatch
    ):
        """The preferred outcome of section 14: the duplicate gets the result, not an error.

        The waiter is started while the owner is provably mid-generation and is not released until
        the owner commits, so it really does go round the polling branch rather than arriving
        after the fact and reading a finished resource.
        """
        watch = WaiterWatch(monkeypatch)
        setup = sessions()
        profile = make_profile(setup)
        resource = Resource()
        resource.gate = threading.Event()
        owner_session, waiter_session = sessions(), sessions()

        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(run, owner_session, profile.id, resource)
            await_generation(resource)
            waiter = pool.submit(run, waiter_session, profile.id, resource)
            try:
                # Not a sleep: the owner is released once the waiter has provably joined.
                watch.await_joined(1)
            finally:
                resource.gate.set()
            assert owner.result(timeout=60) == "result #1"
            assert waiter.result(timeout=60) == "result #1"

        assert watch.joined == 1, "the waiter never went round the polling branch"
        assert resource.generations == 1, "the waiter must not have generated anything"

    def test_a_resource_deleted_after_it_was_generated_can_be_generated_again(self, db):
        """A succeeded claim is not a promise that the thing still exists.

        The student deleted the job description, or an account sweep ran. Refusing here -- which
        is what the request layer correctly does for a vanished replay target -- would mean the
        student could never build a plan for that posting again.
        """
        profile = make_profile(db)
        resource = Resource()
        run(db, profile.id, resource)
        resource.value = None

        assert run(db, profile.id, resource) == "result #2"
        assert resource.generations == 2
        assert claims(db, profile.id)[0].status == "succeeded"

    def test_the_claim_is_committed_before_the_generation_runs(self, sessions):
        """Phase 19, at this layer: the claim has to be visible to other sessions before the model
        is called, and no transaction may be held while it runs.

        If the claim were only flushed, a second request would not see it and would generate too;
        if a transaction stayed open, a 120-second model call would hold it for 120 seconds.
        """
        setup = sessions()
        profile = make_profile(setup)
        observer = sessions()
        resource = Resource()
        seen = {}

        def observe():
            # The status is copied out as a plain string, not kept as an ORM attribute. A
            # rollback expires the instance, so reading `.status` after this function returns
            # would re-query and report the settled value -- the measurement would quietly
            # become "what is it now" instead of "what was it while the generation ran".
            found = (
                observer.query(OperationClaim)
                .filter(OperationClaim.profile_id == profile.id)
                .one_or_none()
            )
            seen["status"] = None if found is None else str(found.status)
            seen["in_transaction"] = observer.in_transaction()
            observer.rollback()
            return "x", 1

        idempotency.run_business_once(
            setup,
            profile.id,
            operation=OPERATION,
            identity=IDENTITY,
            perform=observe,
            read_back=resource.read_back,
            result_kind="job_preparation_plan",
        )

        assert seen["status"] is not None, "the claim was not visible to another session"
        assert seen["status"] == "claimed", "the claim must be committed, not merely flushed"
        assert not setup.in_transaction(), (
            "a transaction was still open on the claiming session while the generation ran"
        )
# ==========================================================================================
# 4. A repeatable operation: REGENERATE
# ==========================================================================================


class TestRegenerate:
    """The Phase 26 half. For a repeatable operation a settled claim must not answer everybody.

    POST /placement-path/generate is documented as "creates the student's first placement path, or
    regenerates it ... the same operation either way". Collapsing on the claim alone -- the rule
    that is exactly right for a preparation plan -- would mean the student's Generate button
    silently returned the path they already had.
    """

    def test_a_deliberate_second_request_generates_again(self, db):
        profile = make_profile(db)
        resource = Repeatable()

        first = regenerate(db, profile.id, resource)
        second = regenerate(db, profile.id, resource)

        assert (first, second) == ("result #1", "result #2")
        assert resource.generations == 2, "a later deliberate request is a new operation"

    def test_the_claim_is_taken_over_rather_than_accumulated(self, db):
        profile = make_profile(db)
        resource = Repeatable()
        regenerate(db, profile.id, resource)
        regenerate(db, profile.id, resource)

        stored = claims(db, profile.id)
        assert [c.status for c in stored] == ["succeeded"], stored
        assert stored[0].result_id == 2, "the claim points at the newest generation"

    def test_a_request_that_waited_is_answered_from_the_generation_it_watched(
        self, sessions, monkeypatch
    ):
        """The distinction the whole mode rests on, and the reason it is not a time window.

        The waiter overlapped the owner, so it is a duplicate and gets the owner's result. It must
        not start a second generation, and it must not be handed a *later* one either.
        """
        watch = WaiterWatch(monkeypatch)
        setup = sessions()
        profile = make_profile(setup)
        resource = Repeatable()
        resource.gate = threading.Event()
        owner_session, waiter_session = sessions(), sessions()

        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(regenerate, owner_session, profile.id, resource)
            await_generation(resource)
            waiter = pool.submit(regenerate, waiter_session, profile.id, resource)
            try:
                watch.await_joined(1)
            finally:
                resource.gate.set()
            assert owner.result(timeout=60) == "result #1"
            assert waiter.result(timeout=60) == "result #1"

        # Load-bearing for REGENERATE: the waiter is owed the owner's result *because* it
        # overlapped, and this is the assertion that proves it overlapped.
        assert watch.joined == 1, "the waiter never went round the polling branch"
        assert resource.generations == 1, "the waiter must not have generated anything"

    def test_concurrent_requests_that_all_joined_generate_once(self, sessions, monkeypatch):
        """Three requests that provably overlap collapse to one generation.

        The old version of this test used a start barrier and then released the owner the moment
        it entered `perform`, which is the instant *before* the followers have read the claim.
        When the scheduler cooperated it passed; when it did not, a follower arrived after
        settlement and correctly regenerated, and the test reported that as a failure of
        coalescing. It failed in 1 of 15 isolated runs and once in a full suite run.

        A start barrier cannot fix that, because what matters is not when the followers *start*
        but whether they reach the claim check while the claim is still in flight. So the owner
        is now held until both followers have registered as waiting -- see [WaiterWatch] for why
        that signal is the production act rather than a proxy for it.
        """
        watch = WaiterWatch(monkeypatch)
        setup = sessions()
        profile = make_profile(setup)
        resource = Repeatable()
        resource.gate = threading.Event()

        def attempt():
            return regenerate(sessions(), profile.id, resource)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(attempt) for _ in range(3)]
            try:
                await_generation(resource)
                # One owner inside `perform`, two followers that have each read the claim as in
                # flight. Only now may the owner settle.
                watch.await_joined(2)
            finally:
                resource.gate.set()
            results = [f.result(timeout=60) for f in futures]

        # Asserted before the count, because it is what makes the count mean anything: without
        # it, "generated once" could equally describe a run in which two requests never
        # overlapped at all.
        assert watch.joined == 2, (
            f"{watch.joined} of 2 followers joined the owner's generation, so this run did not "
            "exercise coalescing"
        )
        assert resource.generations == 1, (
            f"three overlapping requests generated {resource.generations} times"
        )
        assert results == ["result #1"] * 3

        # Both halves in one run, as the original test intended: coalescing three overlapping
        # requests must not have left the resource unable to regenerate for a later deliberate
        # one. Getting one right by breaking the other is the trap.
        resource.gate = None
        assert regenerate(sessions(), profile.id, resource) == "result #2"
        assert resource.generations == 2, "and the race must not have frozen the resource"

    def test_a_request_arriving_after_settlement_regenerates(self, sessions):
        """The other half, kept separate: a request that did not overlap is not a duplicate.

        This is the behaviour the old test intermittently caught and mislabelled a bug. It is the
        documented `on_settled=REGENERATE` policy -- the student pressed Generate again and is
        owed a new generation, not a replay of the old one -- and it is deterministic here
        because the owner is fully settled before the second request is submitted.
        """
        setup = sessions()
        profile = make_profile(setup)
        resource = Repeatable()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(regenerate, sessions(), profile.id, resource)
            assert first.result(timeout=60) == "result #1"
            # Settled, not merely started: no overlap is possible from here.
            assert [c.status for c in claims(setup, profile.id)] == ["succeeded"]

            second = pool.submit(regenerate, sessions(), profile.id, resource)
            assert second.result(timeout=60) == "result #2"

        assert resource.generations == 2, "a request that never overlapped must generate again"

    def test_a_failed_generation_is_still_retryable(self, db):
        profile = make_profile(db)
        resource = Repeatable()
        resource.fail = True
        with pytest.raises(RuntimeError):
            regenerate(db, profile.id, resource)
        assert claims(db, profile.id)[0].status == "failed"

        resource.fail = False
        assert regenerate(db, profile.id, resource) == "result #2"

    def test_two_profiles_regenerate_independently(self, db):
        first = make_profile(db)
        second = make_profile(db)
        first_resource, second_resource = Repeatable(), Repeatable()

        regenerate(db, first.id, first_resource)
        regenerate(db, second.id, second_resource)

        assert (first_resource.generations, second_resource.generations) == (1, 1)
        keys = {claims(db, first.id)[0].idempotency_key, claims(db, second.id)[0].idempotency_key}
        assert len(keys) == 2, f"each profile derives its own business key: {keys}"

    def test_replay_mode_would_have_blocked_the_regeneration(self, db):
        """The mutation this mode exists to prevent, asserted as a property rather than a comment.

        Same resource, same identity, REPLAY instead of REGENERATE: the second deliberate request
        is answered from the first and the model never runs again. That is correct for a
        preparation plan and is the silent death of the Generate button here.
        """
        profile = make_profile(db)
        resource = Resource()  # reads back by identity, as the REPLAY contract expects

        first = run(db, profile.id, resource, on_settled=idempotency.REPLAY)
        second = run(db, profile.id, resource, on_settled=idempotency.REPLAY)

        assert (first, second) == ("result #1", "result #1")
        assert resource.generations == 1
