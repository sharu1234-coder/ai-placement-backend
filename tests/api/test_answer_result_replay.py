"""Phase 31: a resent answer gets its result back, and a different answer is still refused.

The four answer endpoints were already safe in the way Phase 30 measured -- one durable answer, no
duplicate AI. What they did with the *second* request was answer 409, and the Android client turns
any 409 into "That changed while you were working. Please refresh and try again." So a student
whose answer committed and whose response was lost was shown a failure for work that had
succeeded; on the voice screen that was worse, because VoiceInterviewManager deletes the recording
in a `finally` and the error state never returns to READY_TO_RECORD, so there was nothing left to
resend.

The fix is server-side and needs no client change: the whole success response of all four
endpoints is reconstructible from durable state, so a resend is answered from what was stored.
Identity is the submitted answer itself, which is exact for the text endpoints -- OkHttp resends
the same body byte for byte, so a resend always matches and an edited answer never does. Audio is
the one weaker case and says so in `_is_replay_of`.

What these tests have to hold down is both halves. Reconciling too little leaves the false
failure; reconciling too much ("any 409 on an answered question is fine") would silently accept a
*different* answer and tell the student it was stored when it was not. Every endpoint therefore
carries a conflict control next to its replay test.
"""

import io

import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.assessment import GeneratedQuestion
from app.ai.schemas.interview import GeneratedInterviewEvaluation, GeneratedInterviewQuestion
from app.database.base import Base
from app.main import app
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.interview_answer import InterviewAnswer
from app.models.question_attempt import QuestionAttempt
from app.models.recruiter_attack_session import RecruiterAttackSession
from app.models.recruiter_attack_turn import RecruiterAttackTurn
from tests.fixtures import replay_harness as h

TRANSCRIPT = "This is the transcribed answer."
MCQ_ANSWER = "Structured Query Language"

#: `ftyp` has to sit at bytes 4..8 or the route rejects the upload as not an audio recording.
AUDIO_BYTES = bytes([0, 0, 0, 0x18]) + b"ftypM4A fake-recording"


class CountingProvider(AIProvider):
    """Deterministic content, and a count of how much model work each request caused.

    The generated question is an mcq on purpose: that path grades deterministically and never
    calls the model, so `calls` measures only the work a replay could duplicate.
    """

    def __init__(self):
        self.calls = 0

    def generate_structured(self, prompt, schema):
        self.calls += 1
        if schema is GeneratedQuestion:
            return GeneratedQuestion(
                type="mcq",
                prompt="What does SQL stand for?",
                options=[MCQ_ANSWER, "Simple Query Language"],
                expected_answer_or_rubric=MCQ_ANSWER,
                explanation="SQL is Structured Query Language.",
            )
        if schema is GeneratedInterviewQuestion:
            return GeneratedInterviewQuestion(
                question_text="Tell me about a project.", based_on="Project: Payments"
            )
        if schema is GeneratedInterviewEvaluation:
            return GeneratedInterviewEvaluation(
                technical_depth=0.7, communication=0.7, structure=0.7,
                confidence=0.7, feedback_text="Fine.",
            )
        raise AssertionError(f"unexpected schema {schema}")

    def generate_text(self, prompt):
        self.calls += 1
        return "fake text"

    def transcribe(self, audio: bytes) -> str:
        self.calls += 1
        return TRANSCRIPT

    def synthesize_speech(self, text: str) -> bytes:
        return b"FAKEWAV"


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "answer_replay.db")
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def provider():
    counting = CountingProvider()
    app.dependency_overrides[get_ai_provider] = lambda: counting
    yield counting
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def client_and_sessions(engine, tmp_path, monkeypatch, provider):
    client, sessions = h.make_client(engine, tmp_path, monkeypatch)
    with client as test_client:
        yield test_client, sessions
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _disarm():
    yield
    h.disarm_gate()


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def _start_assessment(client, db, headers) -> tuple[int, int]:
    blueprint = AssessmentBlueprint(
        role_id=None, jd_id=None, profile_id=h.profile_id(db), source="phase31",
        sections=[{"skill": "SQL", "assessment_type": "sql", "weight": 1.0,
                   "question_count": 2, "is_gap": False}],
    )
    db.add(blueprint)
    db.commit()
    started = client.post("/assessments", json={"blueprint_id": blueprint.id}, headers=headers)
    assert started.status_code == 201, started.text
    assessment_id = started.json()["id"]
    question_id = _next_assessment_question(client, assessment_id, headers)
    return assessment_id, question_id


def _next_assessment_question(client, assessment_id: int, headers) -> int:
    nxt = client.get(f"/assessments/{assessment_id}/next-question", headers=headers)
    assert nxt.status_code == 200, nxt.text
    return nxt.json()["question"]["id"]


def _start_interview(client, headers) -> tuple[int, int]:
    started = client.post(
        "/interviews", json={"role_id": None, "interview_type": "behavioral"}, headers=headers
    )
    assert started.status_code == 201, started.text
    interview_id = started.json()["id"]
    return interview_id, _next_interview_question(client, interview_id, headers)


def _next_interview_question(client, interview_id: int, headers) -> int:
    nxt = client.get(f"/interviews/{interview_id}/next-question", headers=headers)
    assert nxt.status_code == 200, nxt.text
    return nxt.json()["question"]["id"]


def _seed_attack_turn(db, profile_id: int) -> tuple[int, int]:
    session = RecruiterAttackSession(
        profile_id=profile_id, source_type="resume_claim",
        source_label="Built a payments service", status="in_progress",
    )
    db.add(session)
    db.commit()
    turn = RecruiterAttackTurn(
        session_id=session.id, order_index=1,
        question_text="Which part did you write?", based_on="Built a payments service",
    )
    db.add(turn)
    db.commit()
    return session.id, turn.id


def _audio_file():
    return {"file": ("answer.m4a", io.BytesIO(AUDIO_BYTES), "audio/mp4")}


# ---------------------------------------------------------------------------
# 1. POST /assessments/{id}/answer
# ---------------------------------------------------------------------------


def test_assessment_answer_replay_returns_the_stored_result(client_and_sessions, provider):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    assessment_id, question_id = _start_assessment(client, db, headers)
    body = {"question_id": question_id, "student_answer": MCQ_ANSWER, "time_taken_seconds": 5}

    before = provider.calls
    first = client.post(f"/assessments/{assessment_id}/answer", json=body, headers=headers)
    mid = provider.calls
    replay = client.post(f"/assessments/{assessment_id}/answer", json=body, headers=headers)
    after = provider.calls

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert after - mid == 0, "the replay cost model work"
    db.expire_all()
    assert db.query(QuestionAttempt).filter(
        QuestionAttempt.assessment_id == assessment_id,
        QuestionAttempt.student_answer.isnot(None),
    ).count() == 1
    assert mid - before == 0, "grading an mcq must not call the model at all"


def test_assessment_answer_with_different_text_is_still_a_conflict(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    assessment_id, question_id = _start_assessment(client, db, headers)
    base = {"question_id": question_id, "time_taken_seconds": 5}

    assert client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={**base, "student_answer": MCQ_ANSWER},
    ).status_code == 200
    changed = client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={**base, "student_answer": "Simple Query Language"},
    )

    assert changed.status_code == 409, changed.text
    db.expire_all()
    stored = db.query(QuestionAttempt).filter(
        QuestionAttempt.assessment_id == assessment_id,
        QuestionAttempt.student_answer.isnot(None),
    ).one()
    assert stored.student_answer == MCQ_ANSWER, "the conflict overwrote the stored answer"


def test_assessment_replay_ignores_a_different_time_taken(client_and_sessions):
    """`time_taken_seconds` is metadata about the attempt, not the answer the grade judges, so a
    resend that differs only there is still the same logical operation."""
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    assessment_id, question_id = _start_assessment(client, db, headers)

    first = client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={"question_id": question_id, "student_answer": MCQ_ANSWER, "time_taken_seconds": 5},
    )
    replay = client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={"question_id": question_id, "student_answer": MCQ_ANSWER, "time_taken_seconds": 9},
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()


def test_a_second_assessment_question_is_unaffected(client_and_sessions):
    """Reconciliation is per question. Answering the next one is a new operation, not a replay."""
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    assessment_id, first_question = _start_assessment(client, db, headers)
    assert client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={"question_id": first_question, "student_answer": MCQ_ANSWER, "time_taken_seconds": 1},
    ).status_code == 200

    second_question = _next_assessment_question(client, assessment_id, headers)
    assert second_question != first_question
    answered = client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={"question_id": second_question, "student_answer": MCQ_ANSWER, "time_taken_seconds": 1},
    )

    assert answered.status_code == 200, answered.text
    db.expire_all()
    assert db.query(QuestionAttempt).filter(
        QuestionAttempt.assessment_id == assessment_id,
        QuestionAttempt.student_answer.isnot(None),
    ).count() == 2


def test_another_student_cannot_replay_into_this_assessment(client_and_sessions):
    client, sessions = client_and_sessions
    owner = h.signup(client, "owner31@example.com")
    db = sessions()
    assessment_id, question_id = _start_assessment(client, db, owner)
    body = {"question_id": question_id, "student_answer": MCQ_ANSWER, "time_taken_seconds": 5}
    assert client.post(f"/assessments/{assessment_id}/answer", json=body, headers=owner).status_code == 200

    intruder = h.signup(client, "intruder31@example.com")
    stolen = client.post(f"/assessments/{assessment_id}/answer", json=body, headers=intruder)

    assert stolen.status_code == 404, stolen.text
    assert MCQ_ANSWER not in stolen.text


def test_concurrent_identical_assessment_answers_store_one_and_both_succeed(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    assessment_id, question_id = _start_assessment(client, db, headers)

    responses = h.post_together(
        client, f"/assessments/{assessment_id}/answer", headers,
        # The UPDATE itself: a request only reaches it having passed its own "still pending" read,
        # and none can commit while the barrier holds them, so this forces the exact overlap where
        # the conditional UPDATE decides the winner.
        gate="UPDATE question_attempts",
        body={"question_id": question_id, "student_answer": MCQ_ANSWER, "time_taken_seconds": 5},
    )

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    db.expire_all()
    assert db.query(QuestionAttempt).filter(
        QuestionAttempt.assessment_id == assessment_id,
        QuestionAttempt.student_answer.isnot(None),
    ).count() == 1


# ---------------------------------------------------------------------------
# 2. POST /interviews/{id}/answer
# ---------------------------------------------------------------------------


def test_interview_answer_replay_returns_the_stored_answer(client_and_sessions, provider):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    interview_id, question_id = _start_interview(client, headers)
    body = {"interview_question_id": question_id, "answer_text": "I built it end to end."}

    first = client.post(f"/interviews/{interview_id}/answer", json=body, headers=headers)
    mid = provider.calls
    replay = client.post(f"/interviews/{interview_id}/answer", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert provider.calls - mid == 0
    db.expire_all()
    assert db.query(InterviewAnswer).filter(
        InterviewAnswer.interview_question_id == question_id
    ).count() == 1


def test_interview_answer_with_different_text_is_still_a_conflict(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    interview_id, question_id = _start_interview(client, headers)

    assert client.post(
        f"/interviews/{interview_id}/answer", headers=headers,
        json={"interview_question_id": question_id, "answer_text": "First answer."},
    ).status_code == 200
    changed = client.post(
        f"/interviews/{interview_id}/answer", headers=headers,
        json={"interview_question_id": question_id, "answer_text": "A different answer."},
    )

    assert changed.status_code == 409, changed.text
    db.expire_all()
    assert db.query(InterviewAnswer).filter(
        InterviewAnswer.interview_question_id == question_id
    ).one().answer_text == "First answer."


def test_concurrent_identical_interview_answers_store_one_and_both_succeed(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    interview_id, question_id = _start_interview(client, headers)

    responses = h.post_together(
        client, f"/interviews/{interview_id}/answer", headers,
        # The insert is the seam: the loser hits the unique index here, which is where the
        # reconciliation it now gets instead of a 409 lives.
        gate="INSERT INTO interview_answers",
        body={"interview_question_id": question_id, "answer_text": "I built it end to end."},
    )

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    db.expire_all()
    assert db.query(InterviewAnswer).filter(
        InterviewAnswer.interview_question_id == question_id
    ).count() == 1


# ---------------------------------------------------------------------------
# 3. POST /interviews/{id}/answer/audio
# ---------------------------------------------------------------------------


def test_audio_answer_replay_returns_the_stored_transcript_without_retranscribing(
    client_and_sessions, provider
):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    interview_id, question_id = _start_interview(client, headers)
    data = {"interview_question_id": str(question_id)}

    before = provider.calls
    first = client.post(
        f"/interviews/{interview_id}/answer/audio", headers=headers, data=data, files=_audio_file()
    )
    mid = provider.calls
    replay = client.post(
        f"/interviews/{interview_id}/answer/audio", headers=headers, data=data, files=_audio_file()
    )
    after = provider.calls

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert first.json()["answer_text"] == TRANSCRIPT
    assert mid - before == 1, "the first upload should have transcribed exactly once"
    assert after - mid == 0, "the replay retranscribed -- the expensive work was duplicated"
    db.expire_all()
    assert db.query(InterviewAnswer).filter(
        InterviewAnswer.interview_question_id == question_id
    ).count() == 1


def test_an_audio_answer_does_not_reconcile_a_text_answer(client_and_sessions, provider):
    """Different modes are different operations, so a mode mismatch stays a conflict. This is the
    guard that keeps audio's weaker identity from leaking into the text path."""
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    interview_id, question_id = _start_interview(client, headers)

    assert client.post(
        f"/interviews/{interview_id}/answer", headers=headers,
        json={"interview_question_id": question_id, "answer_text": "Typed answer."},
    ).status_code == 200
    before = provider.calls
    by_audio = client.post(
        f"/interviews/{interview_id}/answer/audio", headers=headers,
        data={"interview_question_id": str(question_id)}, files=_audio_file(),
    )

    assert by_audio.status_code == 409, by_audio.text
    assert provider.calls - before == 0, "a refused audio answer still paid for transcription"
    db.expire_all()
    assert db.query(InterviewAnswer).filter(
        InterviewAnswer.interview_question_id == question_id
    ).one().answer_text == "Typed answer."


def test_a_text_answer_does_not_reconcile_an_audio_answer(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    interview_id, question_id = _start_interview(client, headers)

    assert client.post(
        f"/interviews/{interview_id}/answer/audio", headers=headers,
        data={"interview_question_id": str(question_id)}, files=_audio_file(),
    ).status_code == 200
    # Submitted as the transcript itself, which is the hardest version of this: even text that
    # matches the stored answer exactly is not a replay of the audio operation that produced it.
    by_text = client.post(
        f"/interviews/{interview_id}/answer", headers=headers,
        json={"interview_question_id": question_id, "answer_text": TRANSCRIPT},
    )

    assert by_text.status_code == 409, by_text.text
    db.expire_all()
    stored = db.query(InterviewAnswer).filter(
        InterviewAnswer.interview_question_id == question_id
    ).one()
    assert stored.transcribed is True


# ---------------------------------------------------------------------------
# 4. POST /recruiter-attack/sessions/{id}/answer
# ---------------------------------------------------------------------------


def test_attack_answer_replay_returns_the_stored_turn(client_and_sessions, provider):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    session_id, turn_id = _seed_attack_turn(db, h.profile_id(db))
    body = {"turn_id": turn_id, "answer_text": "I wrote the ledger."}

    first = client.post(f"/recruiter-attack/sessions/{session_id}/answer", json=body, headers=headers)
    mid = provider.calls
    replay = client.post(f"/recruiter-attack/sessions/{session_id}/answer", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert provider.calls - mid == 0
    db.expire_all()
    assert db.get(RecruiterAttackTurn, turn_id).answer_text == "I wrote the ledger."


def test_attack_answer_with_different_text_is_still_a_conflict(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    session_id, turn_id = _seed_attack_turn(db, h.profile_id(db))

    assert client.post(
        f"/recruiter-attack/sessions/{session_id}/answer", headers=headers,
        json={"turn_id": turn_id, "answer_text": "I wrote the ledger."},
    ).status_code == 200
    changed = client.post(
        f"/recruiter-attack/sessions/{session_id}/answer", headers=headers,
        json={"turn_id": turn_id, "answer_text": "Actually someone else did."},
    )

    assert changed.status_code == 409, changed.text
    db.expire_all()
    assert db.get(RecruiterAttackTurn, turn_id).answer_text == "I wrote the ledger."


def test_another_student_cannot_replay_into_this_attack_session(client_and_sessions):
    client, sessions = client_and_sessions
    owner = h.signup(client, "attackowner@example.com")
    db = sessions()
    session_id, turn_id = _seed_attack_turn(db, h.profile_id(db))
    body = {"turn_id": turn_id, "answer_text": "I wrote the ledger."}
    assert client.post(
        f"/recruiter-attack/sessions/{session_id}/answer", json=body, headers=owner
    ).status_code == 200

    intruder = h.signup(client, "attackintruder@example.com")
    stolen = client.post(
        f"/recruiter-attack/sessions/{session_id}/answer", json=body, headers=intruder
    )

    assert stolen.status_code == 404, stolen.text
    assert "ledger" not in stolen.text


def test_concurrent_identical_attack_answers_store_one_and_both_succeed(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    session_id, turn_id = _seed_attack_turn(db, h.profile_id(db))

    responses = h.post_together(
        client, f"/recruiter-attack/sessions/{session_id}/answer", headers,
        gate="UPDATE recruiter_attack_turns",
        body={"turn_id": turn_id, "answer_text": "I wrote the ledger."},
    )

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    db.expire_all()
    assert db.get(RecruiterAttackTurn, turn_id).answer_text == "I wrote the ledger."


# ---------------------------------------------------------------------------
# across a client restart
# ---------------------------------------------------------------------------


def test_a_resend_after_the_client_restarts_still_reconciles(client_and_sessions):
    """What process death looks like from the server: a new connection, a freshly obtained token,
    no memory of the first attempt, and the same answer. Nothing client-side has to survive,
    which is why this phase added no key and no persistence."""
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    interview_id, question_id = _start_interview(client, headers)
    body = {"interview_question_id": question_id, "answer_text": "I built it end to end."}

    first = client.post(f"/interviews/{interview_id}/answer", json=body, headers=headers)
    assert first.status_code == 200, first.text

    relogged = client.post(
        "/auth/login", json={"email": "p30@example.com", "password": h.PASSWORD}
    ).json()["access_token"]
    replay = client.post(
        f"/interviews/{interview_id}/answer",
        json=body,
        headers={"Authorization": f"Bearer {relogged}"},
    )

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    db.expire_all()
    assert db.query(InterviewAnswer).filter(
        InterviewAnswer.interview_question_id == question_id
    ).count() == 1


def test_a_replay_returns_this_question_s_grade_and_not_a_sibling_s(client_and_sessions):
    """Two answered questions in one assessment, graded differently, then each one replayed.

    This exists because of a mutation that survived without it: dropping the question filter from
    the replay read left the lookup picking whichever answered attempt in the assessment came
    first, and every other test had at most one answered question, so nothing noticed. Grading the
    two differently is what makes the mix-up visible -- a student would be shown someone else's
    mark for their own answer, or refused outright.
    """
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    assessment_id, right_question = _start_assessment(client, db, headers)

    correct = client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={"question_id": right_question, "student_answer": MCQ_ANSWER, "time_taken_seconds": 1},
    )
    assert correct.status_code == 200, correct.text
    assert correct.json()["is_correct"] is True

    wrong_question = _next_assessment_question(client, assessment_id, headers)
    wrong = client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={"question_id": wrong_question, "student_answer": "Simple Query Language",
              "time_taken_seconds": 1},
    )
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["is_correct"] is False

    replay_wrong = client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={"question_id": wrong_question, "student_answer": "Simple Query Language",
              "time_taken_seconds": 1},
    )
    replay_correct = client.post(
        f"/assessments/{assessment_id}/answer", headers=headers,
        json={"question_id": right_question, "student_answer": MCQ_ANSWER, "time_taken_seconds": 1},
    )

    assert replay_wrong.status_code == 200, replay_wrong.text
    assert replay_wrong.json() == wrong.json()
    assert replay_correct.status_code == 200, replay_correct.text
    assert replay_correct.json() == correct.json()
