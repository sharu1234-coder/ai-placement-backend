"""Regression for a real audit finding: assessment_run_service.submit_answer previously read
the pending QuestionAttempt, then separately mutated and committed it ("load, mutate, commit")
-- the exact same race class mission_service.complete / subscription_service._activate_paid
already guard against elsewhere with an atomic conditional UPDATE. Two concurrent submissions
for the same pending question could both pass the "still pending" check before either
committed, with the second commit silently overwriting the first's grade. The fix applies the
identical atomic-UPDATE-with-WHERE-still-pending pattern; this test proves it the same way
test_subscription_service.py's own concurrency test does -- two separate DB sessions on one
shared in-memory database (StaticPool), so one session's commit is genuinely invisible to the
other until it re-reads."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.provider import AIProvider
from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.profile import StudentProfile  # noqa: F401  (registers the FK target table)
from app.models.question import Question
from app.models.question_attempt import QuestionAttempt
from app.services import assessment_run_service as svc

PROFILE_ID = 1


class UnusedAIProvider(AIProvider):
    """An mcq answer is graded deterministically, never via the AI provider -- this fails
    loudly if that ever changes."""

    def generate_structured(self, prompt, schema):
        raise AssertionError("AI provider should not be called for an mcq answer")

    def generate_text(self, prompt):
        raise AssertionError("AI provider should not be called for an mcq answer")


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _seed(db):
    blueprint = AssessmentBlueprint(profile_id=PROFILE_ID, source="role", sections=[])
    db.add(blueprint)
    db.flush()
    assessment = Assessment(profile_id=PROFILE_ID, blueprint_id=blueprint.id, status="in_progress")
    db.add(assessment)
    db.flush()
    question = Question(
        skill_tag="SQL", type="mcq", difficulty="medium", prompt="What does SQL stand for?",
        options=["Structured Query Language", "Something Else"],
        expected_answer_or_rubric="Structured Query Language", explanation="It's Structured Query Language.",
    )
    db.add(question)
    db.flush()
    attempt = QuestionAttempt(assessment_id=assessment.id, question_id=question.id)
    db.add(attempt)
    db.commit()
    return assessment.id, question.id, attempt.id


def test_submit_answer_rejects_a_second_submission_for_the_same_question(db):
    assessment_id, question_id, _ = _seed(db)
    svc.submit_answer(
        db, PROFILE_ID, assessment_id, question_id, "Structured Query Language", None, UnusedAIProvider()
    )

    with pytest.raises(svc.QuestionNotPendingError):
        svc.submit_answer(db, PROFILE_ID, assessment_id, question_id, "Something Else", None, UnusedAIProvider())


def test_concurrent_submit_answer_loses_the_atomic_update_race_without_corrupting_the_grade(db):
    """Session B answers first and commits; session A's in-memory view of the attempt is
    still "pending" (a real concurrent request's exact state). Session A's own submit_answer
    call must have its atomic conditional UPDATE affect zero rows and raise
    QuestionNotPendingError -- never silently overwrite session B's already-persisted,
    already-correct answer/grade with its own (here, wrong) answer."""
    assessment_id, question_id, attempt_id = _seed(db)

    session_b = sessionmaker(bind=db.get_bind())()
    try:
        winner = svc.submit_answer(
            session_b, PROFILE_ID, assessment_id, question_id,
            "Structured Query Language", None, UnusedAIProvider(),
        )
    finally:
        session_b.close()

    assert winner.is_correct is True
    assert winner.score == 1.0

    # `db`'s own in-memory copy of the attempt still believes it's pending -- exactly the
    # state a truly concurrent second request would be in.
    with pytest.raises(svc.QuestionNotPendingError):
        svc.submit_answer(db, PROFILE_ID, assessment_id, question_id, "Something Else", None, UnusedAIProvider())

    # Re-fetch via a fresh session to prove the winner's grade was actually persisted and
    # never touched by the losing request, not just correct in the winner's own return value.
    session_c = sessionmaker(bind=db.get_bind())()
    try:
        attempt = session_c.get(QuestionAttempt, attempt_id)
        assert attempt.student_answer == "Structured Query Language"
        assert attempt.is_correct is True
        assert attempt.score == 1.0
    finally:
        session_c.close()


def _seed_short_answer(db):
    """Same shape as _seed, but a short-answer question -- the only kind that reaches the AI
    grader, and therefore the only kind with a real gap between the pending check and the write."""
    blueprint = AssessmentBlueprint(profile_id=PROFILE_ID, source="role", sections=[])
    db.add(blueprint)
    db.flush()
    assessment = Assessment(profile_id=PROFILE_ID, blueprint_id=blueprint.id, status="in_progress")
    db.add(assessment)
    db.flush()
    question = Question(
        skill_tag="SQL", type="short_answer", difficulty="medium", prompt="What is a primary key?",
        options=None, expected_answer_or_rubric="A column that uniquely identifies a row.",
        explanation="Primary keys identify rows.",
    )
    db.add(question)
    db.flush()
    attempt = QuestionAttempt(assessment_id=assessment.id, question_id=question.id)
    db.add(attempt)
    db.commit()
    return assessment.id, question.id, attempt.id


def test_an_answer_persisted_while_the_ai_was_grading_is_not_overwritten(db):
    """The race the F12-07 boundary widened, and the one the conditional UPDATE exists for.

    Grading now runs with no transaction open, so the gap between "this question is still
    pending" and "write the grade" is a whole model call wide. This makes something else answer
    the question *inside* that gap: the pending check has already passed, so nothing but the
    conditional UPDATE can reject the write. The earlier concurrency test cannot reach that -- it
    re-reads before the check and is turned away there instead.
    """
    assessment_id, question_id, attempt_id = _seed_short_answer(db)
    engine = db.get_bind()

    class AnswersFromAnotherSessionWhileGrading(AIProvider):
        """Stands in for a concurrent request that lands while the model is working."""

        def generate_structured(self, prompt, schema):
            other = sessionmaker(bind=engine)()
            try:
                attempt = other.get(QuestionAttempt, attempt_id)
                attempt.student_answer = "the winner's answer"
                attempt.is_correct = True
                attempt.score = 1.0
                other.commit()
            finally:
                other.close()
            return schema(is_correct=True, score=0.2, feedback="graded late")

        def generate_text(self, prompt):
            raise AssertionError("not used")

    with pytest.raises(svc.QuestionNotPendingError):
        svc.submit_answer(
            db, PROFILE_ID, assessment_id, question_id,
            "the loser's answer", None, AnswersFromAnotherSessionWhileGrading(),
        )

    verify = sessionmaker(bind=engine)()
    try:
        attempt = verify.get(QuestionAttempt, attempt_id)
        assert attempt.student_answer == "the winner's answer"
        assert attempt.score == 1.0
    finally:
        verify.close()
