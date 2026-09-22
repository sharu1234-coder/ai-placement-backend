"""Two requests for the same finished interview's result, arriving at once.

GET /interviews/{id}/evaluation creates the evaluation row lazily on first read, so its shape
is "SELECT, find nothing, INSERT" -- and two concurrent first reads both find nothing. The
uniqueness of an evaluation is enforced by interview_evaluations.interview_id's unique index,
not by that SELECT, so the loser's INSERT hit the index and escaped as a 500 on a student
asking for the result of an interview they had just finished. Same race, and the same
resolution, as the answer-submission race in test_interview_service_hardening.py (S3).

Three things about the setup are load-bearing, and the first two are the two spelled out in
tests/unit/test_job_hunt_entitlement_concurrency.py.

*Real threads on real sessions.* Calling get_evaluation twice in a row proves nothing here: the
second call finds the first one's row and returns it from the cache, which reaches none of the
recovery being tested.

*A file-backed database, never conftest's in-memory one.* That fixture uses StaticPool, which
hands every session the SAME connection -- so two "concurrent" sessions would share one
transaction, each would see the other's uncommitted INSERT, the unique index would never fire,
and the test would pass no matter what the code did.

*A barrier inside the fake provider.* The AI call sits between the SELECT and the INSERT, so
releasing every thread from inside generate_structured guarantees all of them have already read
"this interview has no evaluation" and none of them has inserted yet. That is what makes the
race certain rather than a matter of scheduling luck.

SQLite serialises writers, so what is demonstrated is not parallel execution -- it is that the
*outcome* is decided by the unique index rather than by a Python-side check, and therefore holds
however the two transactions happen to interleave. On PostgreSQL the same index does the same
job with genuine parallelism.
"""

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- registers every model with Base.metadata
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.interview import GeneratedInterviewEvaluation, GeneratedInterviewQuestion
from app.database.base import Base
from app.models.interview_evaluation import InterviewEvaluation
from app.models.profile import StudentProfile
from app.models.weakness_record import WeaknessRecord
from app.services import interview_service as svc

_SCORE_COLUMNS = (
    "technical_correctness", "relevance", "completeness", "reasoning",
    "structure", "clarity", "role_jd_coverage", "overall_score",
)


class _SetupAIProvider(AIProvider):
    """Drives an interview to completion. No barrier -- the setup is single-threaded."""

    def __init__(self):
        self.question_calls = 0

    def generate_structured(self, prompt, schema):
        if schema is GeneratedInterviewQuestion:
            self.question_calls += 1
            return GeneratedInterviewQuestion(
                question_text=f"How would you index this table? (#{self.question_calls})", based_on="SQL"
            )
        raise AssertionError(f"Unexpected schema requested during setup: {schema}")

    def generate_text(self, prompt):
        return "fake"


class GradingRacer(AIProvider):
    """A real grade, returned only once every racer has reached this point.

    reasoning sits below weakness_service's 0.6 threshold on purpose: a *successful* grade then
    leaves exactly one WeaknessRecord, which is what lets a test prove the losers did not re-run
    the Interview Memory loop.
    """

    def __init__(self, barrier: threading.Barrier):
        self.barrier = barrier
        self.evaluation_calls = 0
        self._lock = threading.Lock()

    def generate_structured(self, prompt, schema):
        if schema is GeneratedInterviewEvaluation:
            with self._lock:
                self.evaluation_calls += 1
            self.barrier.wait(timeout=30)
            return GeneratedInterviewEvaluation(
                technical_correctness=0.8, relevance=0.8, completeness=0.7, reasoning=0.3,
                structure=0.7, clarity=0.8, grammar=0.9, role_jd_coverage=0.65,
                overall_score=0.76, feedback="A real grade.",
            )
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake"


class FailingRacer(AIProvider):
    """Every racer's AI review fails, once they have all arrived -- the ungraded insert race."""

    def __init__(self, barrier: threading.Barrier):
        self.barrier = barrier

    def generate_structured(self, prompt, schema):
        if schema is GeneratedInterviewEvaluation:
            self.barrier.wait(timeout=30)
            raise AIGenerationError("Could not reach the model")
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake"


@pytest.fixture()
def engine(tmp_path):
    # timeout raises SQLite's own lock wait above the 5s default: several threads contending for
    # the write lock on a slow machine would otherwise surface as "database is locked", which is
    # a property of the fixture and not of the code under test.
    engine = create_engine(f"sqlite:///{tmp_path / 'evaluation_race.db'}", connect_args={"timeout": 30})
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def sessions(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def completed(sessions):
    """A finished interview with a real transcript, and the id of the profile that owns it."""
    db = sessions()
    try:
        profile = StudentProfile(user_id=1)
        db.add(profile)
        db.commit()
        db.refresh(profile)
        ai = _SetupAIProvider()
        interview = svc.start_interview(db, profile.id, "technical", "text", None)
        while True:
            response = svc.get_next_question(db, profile.id, interview.id, ai)
            if response.completed:
                break
            svc.submit_answer(db, profile.id, interview.id, response.question.id, "A real answer.")
        return profile.id, interview.id
    finally:
        db.close()


def race(sessions, count, work):
    """Run `work(db, index)` in `count` threads, each on its own Session.

    Returns a list of (result, exception) in thread order. Exceptions are captured rather than
    raised so a failure is data the test can assert on and print -- here every thread is supposed
    to succeed, so a captured exception is exactly the regression being guarded.
    """
    outcomes = [None] * count

    def run(index):
        db = sessions()
        try:
            outcomes[index] = (work(db, index), None)
        except Exception as exc:  # noqa: BLE001 -- the failure is the thing being asserted on
            outcomes[index] = (None, exc)
        finally:
            db.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


def _stored(sessions, interview_id):
    db = sessions()
    try:
        return db.query(InterviewEvaluation).filter(InterviewEvaluation.interview_id == interview_id).all()
    finally:
        db.close()


# ======================================================================================
# The race itself
# ======================================================================================


@pytest.mark.parametrize("racers", [2, 5])
def test_concurrent_first_evaluations_leave_exactly_one_row_and_no_500(sessions, completed, racers):
    """The regression. Every racer passes the "no evaluation yet" read before any of them
    inserts; one insert lands, the rest lose the unique index and must recover into the winner's
    row rather than raising.

    Five as well as two because a check-then-act implementation can get lucky with two threads on
    a machine that happens to schedule them apart, and cannot get lucky five times.
    """
    profile_id, interview_id = completed
    provider = GradingRacer(threading.Barrier(racers))

    outcomes = race(
        sessions, racers, lambda db, _index: svc.get_evaluation(db, profile_id, interview_id, provider)
    )

    errors = [exc for _result, exc in outcomes if exc is not None]
    assert errors == [], f"a concurrent evaluation request failed: {errors!r}"

    results = [result for result, _exc in outcomes]
    assert all(result.is_graded is True for result in results)
    # Every caller was shown the same evaluation -- not two different answers to one question.
    assert {result.overall_score for result in results} == {0.76}
    assert {result.feedback for result in results} == {"A real grade."}

    stored = _stored(sessions, interview_id)
    assert len(stored) == 1, "the losers must not have inserted a second evaluation"
    assert stored[0].overall_score == 0.76
    assert stored[0].feedback_text == "A real grade."

    # Every racer really did reach the model, i.e. every one of them passed the "no evaluation
    # exists" read -- without which the race would not have been exercised at all.
    assert provider.evaluation_calls == racers


@pytest.mark.parametrize("racers", [2, 5])
def test_only_the_race_winner_teaches_interview_memory(sessions, completed, racers):
    """The side-effect half. Interview Memory is written after the evaluation's own commit, so a
    loser that carried on past the failed insert would record every dimension of the same
    interview a second time -- inflating `occurrences`, which is the count the status machine
    advances a weakness on. The result must be indistinguishable from one request arriving alone.
    """
    profile_id, interview_id = completed
    provider = GradingRacer(threading.Barrier(racers))

    race(sessions, racers, lambda db, _index: svc.get_evaluation(db, profile_id, interview_id, provider))

    db = sessions()
    try:
        records = db.query(WeaknessRecord).filter(WeaknessRecord.profile_id == profile_id).all()
    finally:
        db.close()
    assert [record.skill_key for record in records] == ["interview:reasoning"]
    assert records[0].occurrences == 1, "a losing request re-ran the Interview Memory loop"


@pytest.mark.parametrize("racers", [2, 5])
def test_concurrent_failing_evaluations_leave_exactly_one_ungraded_row(sessions, completed, racers):
    """The second insert site: when the AI review fails, Phase 2C writes an ungraded row rather
    than raising, and that write races in exactly the same way. It must stay honest under the
    race -- one row, every score still NULL, and nothing learned from it."""
    profile_id, interview_id = completed
    provider = FailingRacer(threading.Barrier(racers))

    outcomes = race(
        sessions, racers, lambda db, _index: svc.get_evaluation(db, profile_id, interview_id, provider)
    )

    errors = [exc for _result, exc in outcomes if exc is not None]
    assert errors == [], f"a concurrent evaluation request failed: {errors!r}"

    results = [result for result, _exc in outcomes]
    assert all(result.is_graded is False for result in results)
    assert all(result.overall_score is None for result in results)

    stored = _stored(sessions, interview_id)
    assert len(stored) == 1
    assert all(getattr(stored[0], column) is None for column in _SCORE_COLUMNS)

    db = sessions()
    try:
        assert db.query(WeaknessRecord).filter(WeaknessRecord.profile_id == profile_id).count() == 0
    finally:
        db.close()


def test_a_mixed_race_gives_one_consistent_answer_and_stays_regradable(sessions, completed):
    """One request's AI review succeeds and the other's fails, and they arrive together.

    Which of the two owns the row is genuinely undecidable, so what is asserted is what has to
    hold either way: no 500, exactly one row, and both callers shown the same thing rather than
    two different answers to the same question. If the failing request is the one that won, the
    cost is an ungraded result -- and the last assertion is what makes that acceptable: an
    ungraded row does not short-circuit the cache, so the ordinary next request regrades it in
    place. Losing the race is never permanent and never a second row.
    """
    profile_id, interview_id = completed
    barrier = threading.Barrier(2)
    grading = GradingRacer(barrier)
    failing = FailingRacer(barrier)

    outcomes = race(
        sessions, 2,
        lambda db, index: svc.get_evaluation(db, profile_id, interview_id, grading if index == 0 else failing),
    )

    errors = [exc for _result, exc in outcomes if exc is not None]
    assert errors == [], f"a concurrent evaluation request failed: {errors!r}"

    stored = _stored(sessions, interview_id)
    assert len(stored) == 1
    graded_in_db = stored[0].overall_score is not None
    results = [result for result, _exc in outcomes]
    assert all(result.is_graded is graded_in_db for result in results)
    assert all(result.feedback == stored[0].feedback_text for result in results)

    db = sessions()
    try:
        # Barrier(1) releases immediately -- this is an ordinary, sequential retry.
        final = svc.get_evaluation(db, profile_id, interview_id, GradingRacer(threading.Barrier(1)))
    finally:
        db.close()
    assert final.is_graded is True
    assert final.overall_score == 0.76
    assert len(_stored(sessions, interview_id)) == 1


# ======================================================================================
# The recovery, at the level of the helper that performs it
# ======================================================================================


def test_losing_the_insert_returns_the_winners_row_and_leaves_the_session_usable(sessions, completed):
    """What a losing request reaches, without the threads.

    Three separate requirements of the fix, none of them a by-product of the others: the winner's
    row comes back, the object this call tried to insert is left transient and unstored (so
    nothing half-created can be read from or returned), and the session works afterwards -- the
    failed INSERT poisons the transaction until the rollback, and a session left in that state
    would fail on whatever the request touched next rather than on what actually broke.
    """
    profile_id, interview_id = completed
    winner_session = sessions()
    try:
        svc.get_evaluation(winner_session, profile_id, interview_id, GradingRacer(threading.Barrier(1)))
    finally:
        winner_session.close()

    loser = sessions()
    try:
        duplicate = InterviewEvaluation(interview_id=interview_id, feedback_text="the loser's row")

        returned = svc._insert_evaluation_once(loser, duplicate, interview_id)

        assert returned is not None, "losing the insert must report the winner's row, not None"
        assert returned.feedback_text == "A real grade."
        assert duplicate.id is None, "the rejected object must stay transient and unstored"
        # The session is usable for ordinary work again, not stuck in the failed transaction.
        stored = loser.query(InterviewEvaluation).filter(InterviewEvaluation.interview_id == interview_id).all()
        assert len(stored) == 1
        assert stored[0].feedback_text == "A real grade."
    finally:
        loser.close()


def test_an_integrity_error_that_is_not_the_race_is_not_swallowed(sessions, completed):
    """The guard stays a guard against this one race, not a blanket catch.

    A NOT NULL violation on feedback_text is a programming bug. Recovering from it would leave
    the student looking at an evaluation the code had failed to write, and would hide the bug for
    as long as the code lived -- so when the re-read finds no winner, the original error is
    re-raised. The session is still usable afterwards, because the rollback happens either way.
    """
    _profile_id, interview_id = completed
    db = sessions()
    try:
        broken = InterviewEvaluation(interview_id=interview_id, feedback_text=None)

        with pytest.raises(IntegrityError):
            svc._insert_evaluation_once(db, broken, interview_id)

        count = db.query(InterviewEvaluation).filter(InterviewEvaluation.interview_id == interview_id).count()
        assert count == 0
    finally:
        db.close()


# ======================================================================================
# The second race: concurrent retries of a row that is ALREADY ungraded
# ======================================================================================
#
# A different race on the same endpoint, and it never reaches the INSERT above. The row
# already exists, so every retry takes the UPDATE path, and what they contend for is the
# ungraded -> graded transition rather than the row itself. It produced no error and no
# duplicate row, which is why it outlived the insert fix -- what it produced instead was a
# second run of the Interview Memory loop over the same interview.


class DistinctGradingRacer(AIProvider):
    """A real grade whose identifying values differ per call.

    This is what lets a test tell *which* racer's result was stored, and therefore prove two
    separate things the identical-output provider above cannot: that exactly one of them wrote,
    and that the losers returned the winner's persisted values rather than the ones they
    computed themselves.

    Only overall_score and feedback vary. reasoning stays below weakness_service's 0.6 threshold
    for every racer, so a successful grade leaves exactly one WeaknessRecord whichever racer
    happens to win.
    """

    def __init__(self, barrier: threading.Barrier):
        self.barrier = barrier
        self.evaluation_calls = 0
        self._lock = threading.Lock()

    def generate_structured(self, prompt, schema):
        if schema is GeneratedInterviewEvaluation:
            with self._lock:
                self.evaluation_calls += 1
                call = self.evaluation_calls
            self.barrier.wait(timeout=30)
            return GeneratedInterviewEvaluation(
                technical_correctness=0.8, relevance=0.8, completeness=0.7, reasoning=0.3,
                structure=0.7, clarity=0.8, grammar=0.9, role_jd_coverage=0.65,
                overall_score=0.50 + call / 100.0, feedback=f"Grade from racer {call}.",
            )
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake"


def _make_ungraded(sessions, profile_id, interview_id):
    """Produce the one ungraded row through the real failure path, not by hand-writing it."""
    db = sessions()
    try:
        result = svc.get_evaluation(db, profile_id, interview_id, FailingRacer(threading.Barrier(1)))
    finally:
        db.close()
    assert result.is_graded is False
    stored = _stored(sessions, interview_id)
    assert len(stored) == 1 and stored[0].overall_score is None


def _weakness_records(sessions, profile_id):
    db = sessions()
    try:
        return db.query(WeaknessRecord).filter(WeaknessRecord.profile_id == profile_id).all()
    finally:
        db.close()


@pytest.mark.parametrize("racers", [2, 5])
def test_concurrent_retries_of_an_ungraded_row_grade_it_exactly_once(sessions, completed, racers):
    """The regression, measured before the fix as occurrences=2 / status=REPEATED from a single
    interview -- a student told a weakness keeps recurring because they pressed retry twice.

    Every assertion below is about the same thing from a different side: one transition, one
    persisted grade, one thing learned, and one answer given to every caller.
    """
    profile_id, interview_id = completed
    _make_ungraded(sessions, profile_id, interview_id)
    provider = DistinctGradingRacer(threading.Barrier(racers))

    outcomes = race(
        sessions, racers, lambda db, _index: svc.get_evaluation(db, profile_id, interview_id, provider)
    )

    errors = [exc for _result, exc in outcomes if exc is not None]
    assert errors == [], f"a concurrent retry failed: {errors!r}"

    # Every racer reached the model, so every racer passed the "overall_score IS NULL" read
    # before any of them wrote. Without this the race would not have been exercised at all.
    assert provider.evaluation_calls == racers

    stored = _stored(sessions, interview_id)
    assert len(stored) == 1, "a retry inserted a second evaluation"
    assert stored[0].overall_score is not None, "the row was left ungraded"

    # Exactly one racer's grade is on the row: the losers' UPDATEs matched nothing, so none of
    # them overwrote the winner or half-applied its own values on top.
    expected = {f"Grade from racer {n}." for n in range(1, racers + 1)}
    assert stored[0].feedback_text in expected
    assert stored[0].overall_score == pytest.approx(0.50 + int(stored[0].feedback_text.split()[-1][:-1]) / 100.0)

    # And every caller was handed that same persisted row -- not its own unstored scores.
    results = [result for result, _exc in outcomes]
    assert all(result.is_graded is True for result in results)
    assert {result.feedback for result in results} == {stored[0].feedback_text}
    assert {result.overall_score for result in results} == {stored[0].overall_score}

    records = _weakness_records(sessions, profile_id)
    assert [record.skill_key for record in records] == ["interview:reasoning"]
    assert records[0].occurrences == 1, "a losing retry re-ran the Interview Memory loop"
    assert records[0].status == "NEW", "a second occurrence advanced the weakness status machine"


@pytest.mark.parametrize("racers", [2, 5])
def test_concurrent_retries_that_all_fail_stay_ungraded(sessions, completed, racers):
    """Concurrency hardening must not turn an AI failure into a graded result. The row was
    ungraded before the race and, with every AI call failing, must be ungraded after it: no
    scores, no grade invented by the claim mechanism, and still nothing learned."""
    profile_id, interview_id = completed
    _make_ungraded(sessions, profile_id, interview_id)
    provider = FailingRacer(threading.Barrier(racers))

    outcomes = race(
        sessions, racers, lambda db, _index: svc.get_evaluation(db, profile_id, interview_id, provider)
    )

    errors = [exc for _result, exc in outcomes if exc is not None]
    assert errors == [], f"a concurrent retry failed: {errors!r}"

    results = [result for result, _exc in outcomes]
    assert all(result.is_graded is False for result in results)
    assert all(result.overall_score is None for result in results)

    stored = _stored(sessions, interview_id)
    assert len(stored) == 1
    assert all(getattr(stored[0], column) is None for column in _SCORE_COLUMNS)
    assert _weakness_records(sessions, profile_id) == []


@pytest.mark.parametrize("racers", [2, 5])
def test_concurrent_reads_of_a_graded_evaluation_never_regrade_it(sessions, completed, racers):
    """The cache path, unchanged. A graded evaluation short-circuits before the AI call, so
    concurrent reads of one must not reach the model, must not touch the row, and must not add a
    second occurrence to a weakness the original grade already recorded."""
    profile_id, interview_id = completed
    first = DistinctGradingRacer(threading.Barrier(1))
    db = sessions()
    try:
        graded = svc.get_evaluation(db, profile_id, interview_id, first)
    finally:
        db.close()
    assert graded.is_graded is True

    # Barrier(1) so that a provider which is wrongly called fails the assertions below rather
    # than blocking the test for the full barrier timeout.
    provider = DistinctGradingRacer(threading.Barrier(1))
    outcomes = race(
        sessions, racers, lambda db, _index: svc.get_evaluation(db, profile_id, interview_id, provider)
    )

    errors = [exc for _result, exc in outcomes if exc is not None]
    assert errors == [], f"a concurrent read of a graded evaluation failed: {errors!r}"
    assert provider.evaluation_calls == 0, "a graded evaluation was regraded"

    stored = _stored(sessions, interview_id)
    assert len(stored) == 1
    assert stored[0].feedback_text == graded.feedback
    assert stored[0].overall_score == graded.overall_score
    assert {result.overall_score for result in [r for r, _e in outcomes]} == {graded.overall_score}

    records = _weakness_records(sessions, profile_id)
    assert [record.skill_key for record in records] == ["interview:reasoning"]
    assert records[0].occurrences == 1


def test_an_ordinary_sequential_retry_still_regrades_the_same_row(sessions, completed):
    """Phase 2C's retry, on the file-backed fixture the races use.

    The honesty suite already covers this on the shared in-memory engine; it is repeated here
    because the atomic UPDATE replaced the plain "mutate the loaded object and commit" that
    suite exercised, and this fixture is the one with real per-session connections and real
    transaction boundaries. Ungraded -> graded, one row, one thing learned.
    """
    profile_id, interview_id = completed
    _make_ungraded(sessions, profile_id, interview_id)

    db = sessions()
    try:
        regraded = svc.get_evaluation(db, profile_id, interview_id, DistinctGradingRacer(threading.Barrier(1)))
    finally:
        db.close()

    assert regraded.is_graded is True
    assert regraded.feedback == "Grade from racer 1."
    stored = _stored(sessions, interview_id)
    assert len(stored) == 1
    assert stored[0].overall_score == regraded.overall_score
    assert all(getattr(stored[0], column) is not None for column in _SCORE_COLUMNS)

    records = _weakness_records(sessions, profile_id)
    assert [record.skill_key for record in records] == ["interview:reasoning"]
    assert records[0].occurrences == 1
