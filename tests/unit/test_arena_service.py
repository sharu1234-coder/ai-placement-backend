import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.provider import AIProvider
from app.ai.schemas.assessment import GeneratedQuestion
from app.database.base import Base
from app.models.arena_attempt import ArenaAttempt
from app.models.profile import StudentProfile
from app.services import arena_service as svc
from app.services import assessment_run_service


class FakeAIProvider(AIProvider):
    """Always an MCQ with a fixed correct answer -- deterministic scoring (1.0 for the right
    answer, 0.0 otherwise) without needing the AI-graded short-answer path."""

    def generate_structured(self, prompt, schema):
        return GeneratedQuestion(
            type="mcq", prompt="Pick the right one.", options=["A", "B", "C"],
            expected_answer_or_rubric="A", explanation="A is correct.",
        )

    def generate_text(self, prompt):
        return "fake"


@pytest.fixture()
def engine():
    return create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)


@pytest.fixture()
def db(engine):
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def ai():
    return FakeAIProvider()


_next_user_id = [1]


def _profile(db) -> StudentProfile:
    profile = StudentProfile(user_id=_next_user_id[0])
    _next_user_id[0] += 1
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _answer_all(db, profile_id, assessment_id, ai_provider, correct=True, max_answers=None):
    answered = 0
    while max_answers is None or answered < max_answers:
        response = assessment_run_service.get_next_question(db, profile_id, assessment_id, ai_provider)
        if response.completed:
            break
        assessment_run_service.submit_answer(
            db, profile_id, assessment_id, response.question.id, "A" if correct else "Z", None, ai_provider
        )
        answered += 1
    return answered


def test_list_challenges_have_no_personal_best_before_any_attempt(db):
    profile = _profile(db)
    challenges = svc.list_challenges_for_profile(db, profile.id)
    assert len(challenges) == 5
    assert all(c.personal_best_score is None for c in challenges)


def test_start_attempt_creates_a_real_assessment(db):
    profile = _profile(db)
    start = svc.start_attempt(db, profile.id, "sql_sprint")
    assert start.challenge_key == "sql_sprint"
    assert start.time_limit_seconds == 300

    attempt = db.get(ArenaAttempt, start.arena_attempt_id)
    assert attempt.assessment_id == start.assessment_id
    assert attempt.status == "in_progress"


def test_start_attempt_unknown_challenge_raises(db):
    profile = _profile(db)
    with pytest.raises(svc.ChallengeNotFoundError):
        svc.start_attempt(db, profile.id, "not_a_real_challenge")


def test_complete_before_answering_or_timing_out_raises(db):
    profile = _profile(db)
    start = svc.start_attempt(db, profile.id, "sql_sprint")

    with pytest.raises(svc.ArenaAttemptNotReadyError):
        svc.complete_attempt(db, profile.id, start.arena_attempt_id, time_taken_seconds=10)


def test_completing_after_answering_everything_scores_via_the_real_assessment_engine(db, ai):
    profile = _profile(db)
    start = svc.start_attempt(db, profile.id, "aptitude_blitz")
    _answer_all(db, profile.id, start.assessment_id, ai, correct=True)

    result = svc.complete_attempt(db, profile.id, start.arena_attempt_id, time_taken_seconds=120)
    assert result.score == 1.0
    assert result.timed_out is False
    assert result.is_personal_best is True
    assert result.previous_best_score is None


def test_first_ever_attempt_is_always_a_personal_best(db, ai):
    profile = _profile(db)
    start = svc.start_attempt(db, profile.id, "python_challenge")
    _answer_all(db, profile.id, start.assessment_id, ai, correct=False)

    result = svc.complete_attempt(db, profile.id, start.arena_attempt_id, time_taken_seconds=60)
    assert result.score == 0.0
    assert result.is_personal_best is True  # first attempt ever -- nothing to beat yet


def test_worse_second_attempt_is_not_a_personal_best(db, ai):
    profile = _profile(db)
    first = svc.start_attempt(db, profile.id, "python_challenge")
    _answer_all(db, profile.id, first.assessment_id, ai, correct=True)
    svc.complete_attempt(db, profile.id, first.arena_attempt_id, time_taken_seconds=60)

    second = svc.start_attempt(db, profile.id, "python_challenge")
    _answer_all(db, profile.id, second.assessment_id, ai, correct=False)
    result = svc.complete_attempt(db, profile.id, second.arena_attempt_id, time_taken_seconds=60)

    assert result.score == 0.0
    assert result.is_personal_best is False
    assert result.previous_best_score == 1.0


def test_better_second_attempt_is_a_new_personal_best(db, ai):
    profile = _profile(db)
    first = svc.start_attempt(db, profile.id, "python_challenge")
    _answer_all(db, profile.id, first.assessment_id, ai, correct=False)
    svc.complete_attempt(db, profile.id, first.arena_attempt_id, time_taken_seconds=60)

    second = svc.start_attempt(db, profile.id, "python_challenge")
    _answer_all(db, profile.id, second.assessment_id, ai, correct=True)
    result = svc.complete_attempt(db, profile.id, second.arena_attempt_id, time_taken_seconds=60)

    assert result.is_personal_best is True
    assert result.previous_best_score == 0.0


def test_duplicate_completion_returns_the_same_result_unchanged(db, ai):
    profile = _profile(db)
    start = svc.start_attempt(db, profile.id, "sql_sprint")
    _answer_all(db, profile.id, start.assessment_id, ai, correct=True)

    first = svc.complete_attempt(db, profile.id, start.arena_attempt_id, time_taken_seconds=100)
    second = svc.complete_attempt(db, profile.id, start.arena_attempt_id, time_taken_seconds=999)

    assert second == first  # the second call's (different) time_taken_seconds is discarded


def test_concurrent_completion_only_one_write_lands_no_error(db, engine, ai):
    """Genuinely simulates two near-simultaneous completion requests via two separate DB
    sessions against the same in-memory database, mirroring test_mission_service's equivalent
    race test."""
    profile = _profile(db)
    start = svc.start_attempt(db, profile.id, "sql_sprint")
    _answer_all(db, profile.id, start.assessment_id, ai, correct=True)

    session_b = sessionmaker(bind=engine)()
    try:
        winner = svc.complete_attempt(session_b, profile.id, start.arena_attempt_id, time_taken_seconds=100)
    finally:
        session_b.close()

    # session `db`'s in-memory copy of the attempt is now stale (still "in_progress") --
    # exactly the state a truly concurrent request would be in.
    loser = svc.complete_attempt(db, profile.id, start.arena_attempt_id, time_taken_seconds=250)

    assert loser == winner
    assert db.query(ArenaAttempt).filter(ArenaAttempt.id == start.arena_attempt_id).count() == 1


def test_timeout_force_completes_with_only_partial_answers(db, ai):
    profile = _profile(db)
    start = svc.start_attempt(db, profile.id, "aptitude_blitz")  # 8 questions, 240s limit
    _answer_all(db, profile.id, start.assessment_id, ai, correct=True, max_answers=3)  # stop partway

    attempt = db.get(ArenaAttempt, start.arena_attempt_id)
    attempt.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=241)
    db.commit()

    result = svc.complete_attempt(db, profile.id, start.arena_attempt_id, time_taken_seconds=241)
    assert result.timed_out is True
    assert result.score == 1.0  # scored on the 3 real, correctly-answered questions only


def test_attempt_isolated_per_profile(db, ai):
    profile = _profile(db)
    other = _profile(db)
    start = svc.start_attempt(db, profile.id, "sql_sprint")

    with pytest.raises(svc.ArenaAttemptNotFoundError):
        svc.complete_attempt(db, other.id, start.arena_attempt_id, time_taken_seconds=10)


def test_get_history_unknown_challenge_raises(db):
    profile = _profile(db)
    with pytest.raises(svc.ChallengeNotFoundError):
        svc.get_history(db, profile.id, "not_a_real_challenge")


def test_history_lists_completed_attempts_most_recent_first_with_personal_best_flagged(db, ai):
    profile = _profile(db)
    first = svc.start_attempt(db, profile.id, "sql_sprint")
    _answer_all(db, profile.id, first.assessment_id, ai, correct=False)
    svc.complete_attempt(db, profile.id, first.arena_attempt_id, time_taken_seconds=100)

    second = svc.start_attempt(db, profile.id, "sql_sprint")
    _answer_all(db, profile.id, second.assessment_id, ai, correct=True)
    svc.complete_attempt(db, profile.id, second.arena_attempt_id, time_taken_seconds=90)

    history = svc.get_history(db, profile.id, "sql_sprint")
    assert history.personal_best_score == 1.0
    assert [a.arena_attempt_id for a in history.attempts] == [second.arena_attempt_id, first.arena_attempt_id]
    assert history.attempts[0].is_personal_best is True
    assert history.attempts[1].is_personal_best is False


def test_history_empty_before_any_completed_attempt(db):
    profile = _profile(db)
    history = svc.get_history(db, profile.id, "sql_sprint")
    assert history.attempts == []
    assert history.personal_best_score is None
