"""VA-0.5 item 0.5-5: a student can delete their own interview records.

Answers are retained on purpose so a student can review what they said and how it was
evaluated. This is the other half of that policy: the student decides when a transcript goes.
What is tested here is the part that would be dangerous to get wrong -- ownership, and the
completeness of the delete -- plus the two retention properties the report states as fact:

  * Raw audio is never stored, so a deleted answer leaves nothing behind. Asserted by
    checking `audio_url` is NULL on a stored audio answer, rather than asserting it from the
    absence of a column.
  * There is no automatic expiry. Asserted statically, because the honest way to show that
    nothing purges on a timer is to show no scheduled purge exists.

Real file database with one connection per session, via the shared harness: an in-memory
SQLite engine with StaticPool hands every session the same transaction, which has produced
false passes in this suite before.
"""

import pathlib

import pytest

from app.ai.provider import AIProvider
from app.ai.schemas.interview import GeneratedInterviewQuestion
from app.database.base import Base
from app.main import app
from app.models.interview import Interview
from app.models.interview_answer import InterviewAnswer
from app.models.interview_question import InterviewQuestion
from app.models.profile import StudentProfile
from tests.fixtures import replay_harness as h

ANSWER = "I built the ledger service end to end."


class StubProvider(AIProvider):
    """Deterministic questions; transcription returns a fixed string.

    Declares TRANSCRIBE implicitly by overriding it -- capabilities are derived from the
    methods a provider implements (app/ai/provider.py), so a double cannot misdescribe
    itself and this test needs no capability bookkeeping.
    """

    def generate_structured(self, prompt, schema):
        if schema is GeneratedInterviewQuestion:
            return GeneratedInterviewQuestion(
                question_text="Tell me about a project.", based_on="Project: Payments"
            )
        raise AssertionError(f"unexpected schema {schema.__name__}")

    def generate_text(self, prompt):
        return "text"

    def transcribe(self, audio):
        return ANSWER


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "va05_delete.db")
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client_and_sessions(engine, tmp_path, monkeypatch):
    from app.ai.provider import get_ai_provider

    app.dependency_overrides[get_ai_provider] = lambda: StubProvider()
    client, sessions = h.make_client(engine, tmp_path, monkeypatch)
    with client as test_client:
        yield test_client, sessions
    app.dependency_overrides.clear()


def _start_interview(client, headers):
    created = client.post("/interviews/", headers=headers, json={"type": "behavioral", "mode": "text"})
    assert created.status_code == 201, created.text
    interview_id = created.json()["id"]
    question = client.get(f"/interviews/{interview_id}/next-question", headers=headers)
    assert question.status_code == 200, question.text
    # next-question returns NextInterviewQuestionResponse: the question is nested, and is null
    # once the interview is finished.
    body = question.json()
    assert body["question"] is not None, body
    return interview_id, body["question"]["id"]


def test_a_student_can_delete_their_own_interview_and_its_answers(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client, "delete-own@example.com")
    interview_id, question_id = _start_interview(client, headers)

    answered = client.post(
        f"/interviews/{interview_id}/answer",
        headers=headers,
        json={"interview_question_id": question_id, "answer_text": ANSWER},
    )
    assert answered.status_code == 200, answered.text

    with sessions() as db:
        assert db.query(InterviewAnswer).count() == 1
        assert db.query(InterviewQuestion).count() == 1
        assert db.query(Interview).count() == 1

    deleted = client.delete(f"/interviews/{interview_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    with sessions() as db:
        assert db.query(InterviewAnswer).count() == 0, "the transcript survived the delete"
        assert db.query(InterviewQuestion).count() == 0
        assert db.query(Interview).count() == 0

    # And it is gone from the student's own view, not merely from the tables.
    assert client.get(f"/interviews/{interview_id}/next-question", headers=headers).status_code == 404
    assert client.get("/interviews/", headers=headers).json() == []


def test_deleting_one_interview_leaves_the_students_other_interviews_alone(client_and_sessions):
    """The cascade has to be scoped to one interview, not to the student."""
    client, sessions = client_and_sessions
    headers = h.signup(client, "delete-one@example.com")
    first_id, first_question = _start_interview(client, headers)
    client.post(
        f"/interviews/{first_id}/answer",
        headers=headers,
        json={"interview_question_id": first_question, "answer_text": ANSWER},
    )
    second_id, second_question = _start_interview(client, headers)
    client.post(
        f"/interviews/{second_id}/answer",
        headers=headers,
        json={"interview_question_id": second_question, "answer_text": "A different answer."},
    )

    assert client.delete(f"/interviews/{first_id}", headers=headers).status_code == 204

    with sessions() as db:
        remaining = db.query(InterviewAnswer).all()
        assert len(remaining) == 1
        assert remaining[0].answer_text == "A different answer."
        assert [row.id for row in db.query(Interview).all()] == [second_id]


def test_another_student_cannot_delete_this_interview(client_and_sessions):
    client, sessions = client_and_sessions
    owner = h.signup(client, "owner@example.com")
    interview_id, question_id = _start_interview(client, owner)
    client.post(
        f"/interviews/{interview_id}/answer",
        headers=owner,
        json={"interview_question_id": question_id, "answer_text": ANSWER},
    )

    intruder = h.signup(client, "intruder@example.com")
    refused = client.delete(f"/interviews/{interview_id}", headers=intruder)

    # 404, not 403: the response must not confirm that this id exists.
    assert refused.status_code == 404, refused.text
    assert ANSWER not in refused.text

    with sessions() as db:
        assert db.query(InterviewAnswer).count() == 1, "another student's delete took effect"
        assert db.query(StudentProfile).count() == 2


def test_deleting_an_interview_that_does_not_exist_is_a_404(client_and_sessions):
    client, _sessions = client_and_sessions
    headers = h.signup(client, "missing@example.com")
    assert client.delete("/interviews/424242", headers=headers).status_code == 404


def test_an_audio_answer_stores_a_transcript_and_no_audio(client_and_sessions):
    """The retention claim, asserted against a real stored row.

    If audio_url were ever populated, deleting the row would leave a file behind and the
    "nothing left to delete" part of the policy would be false.
    """
    client, sessions = client_and_sessions
    headers = h.signup(client, "audio-retention@example.com")
    interview_id, question_id = _start_interview(client, headers)

    audio = bytes([0, 0, 0, 0x18]) + b"ftypM4A " + b"\x00" * 64
    posted = client.post(
        f"/interviews/{interview_id}/answer/audio",
        headers=headers,
        data={"interview_question_id": str(question_id)},
        files={"file": ("answer.m4a", audio, "audio/mp4")},
    )
    assert posted.status_code == 200, posted.text

    with sessions() as db:
        stored = db.query(InterviewAnswer).one()
        assert stored.transcribed is True
        assert stored.answer_text == ANSWER
        assert stored.audio_url is None, "a raw-audio reference was persisted"

    assert client.delete(f"/interviews/{interview_id}", headers=headers).status_code == 204
    with sessions() as db:
        assert db.query(InterviewAnswer).count() == 0


def test_no_automatic_purge_of_interview_records_is_scheduled():
    """Retention is "until the student deletes it", and that has to stay true.

    A scheduled purge would silently turn a documented retention promise into a different
    one, so this fails if anything starts expiring interview data on a timer. It is a static
    check because the honest way to show that no timer exists is to show no timer exists.
    """
    service = pathlib.Path("app/services/interview_service.py").read_text(encoding="utf-8")
    for scheduler in ("APScheduler", "BackgroundScheduler", "celery", "add_job", "crontab"):
        assert scheduler.lower() not in service.lower(), (
            f"{scheduler} appeared in interview_service: interview retention is "
            "student-controlled, and an automatic purge schedule is explicitly out of scope "
            "until approved."
        )
    # The only other deletion path is whole-account deletion, which is the student's own act.
    assert "def delete_interview" in service
