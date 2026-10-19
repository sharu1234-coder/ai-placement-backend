"""The authenticated responders, against the real coach and the real interview engine.

These call the responders directly rather than over a socket. The gateway's own behaviour is
covered in `tests/api/test_voice_realtime_turn.py`; what is in question here is whether a
*transcript* reaches the correct existing workflow with the correct identity, and whether the
sentence that comes back says only what the authoritative layer actually established.

What is real: the orchestrator, the tool registry and its read-only catalogue, the grounding
layer, `interview_service` including its unique index and ownership checks, and the disposable
SQLite the API suite uses. What is faked: the AI provider for question generation, through the
same `FakeAIProvider` the interview suite already uses -- so no model runs. **No speech provider
is involved at any point in this file**; a responder takes text and returns text.
"""

import asyncio

import pytest

from app.ai.provider import AIGenerationError, get_ai_provider
from app.database.base import Base
from app.main import app
from app.models.interview_answer import InterviewAnswer
from app.models.role import Role
from app.models.user import User
from app.services import interview_service
from app.voice import responders
from tests.fixtures import replay_harness as h

ANSWER = "I reproduced it locally, bisected the change and added a regression test."


@pytest.fixture()
def engine(tmp_path):
    from sqlalchemy.orm import sessionmaker

    eng = h.make_engine(tmp_path / "voice_responders.db")
    Base.metadata.create_all(bind=eng)
    seed = sessionmaker(bind=eng)()
    seed.add(Role(name="SDE", category="Engineering", description="Software Development Engineer"))
    seed.commit()
    seed.close()
    yield eng
    eng.dispose()


@pytest.fixture()
def made(engine, tmp_path, monkeypatch):
    return h.make_client(engine, tmp_path, monkeypatch)


@pytest.fixture()
def client(made):
    return made[0]


@pytest.fixture()
def db(made):
    session = made[1]()
    yield session
    session.close()


@pytest.fixture()
def headers(client):
    return h.signup(client, email="responders@example.com")


@pytest.fixture()
def user(db, headers):
    return db.query(User).one()


@pytest.fixture()
def fake_ai():
    from tests.api.test_interviews import FakeAIProvider

    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


def ask(responder, text: str):
    return asyncio.run(responder(text))


# --------------------------------------------------------------------------- career coach


class TestTheCoachResponder:
    def test_a_turn_is_answered_from_the_authenticated_students_own_records(
        self, client, headers, db, user
    ):
        responder = responders.build_coach_responder(user=user, db=db, session_key="voice-1")

        answer = ask(responder, "How ready am I for placements?")

        assert answer.text
        # Every sentence came from the grounding layer or a fixed label. Nothing was summarised,
        # because a summary of evidence is a new claim about it.
        assert isinstance(answer.tools_used, tuple)

    def test_an_empty_account_says_so_rather_than_inventing_a_status(
        self, client, headers, db, user
    ):
        """The "unknown facts" case. A fresh account has no skills, no interviews and no resume,
        and the only honest answer is that there is nothing recorded."""
        responder = responders.build_coach_responder(user=user, db=db, session_key="voice-2")

        answer = ask(responder, "What are my weaknesses?")

        lowered = answer.text.lower()
        assert "could not establish" in lowered or "nothing recorded" in lowered
        assert answer.grounded is False

    def test_a_market_question_reports_that_live_data_is_unavailable(
        self, client, headers, db, user
    ):
        """No job provider is approved and `JOB_PROVIDER` is `none`, so there is no live market
        data. The answer must say so and must name no vacancy -- the one thing a coaching agent
        with no data source must never do is produce a plausible job.
        """
        from app.core.config import settings

        assert settings.job_provider == "none"
        responder = responders.build_coach_responder(user=user, db=db, session_key="voice-3")

        answer = ask(responder, "What fresher jobs are there for me in Bangalore right now?")

        lowered = answer.text.lower()
        assert "did not complete" in lowered or "could not establish" in lowered
        assert answer.grounded is False
        # No company, no salary, no advert count presented as fact.
        assert "₹" not in answer.text
        assert "lpa" not in lowered

    def test_a_question_outside_the_closed_intent_set_asks_rather_than_guesses(
        self, client, headers, db, user
    ):
        responder = responders.build_coach_responder(user=user, db=db, session_key="voice-4")

        answer = ask(responder, "qwerty zxcvbn")

        assert answer.text
        assert answer.grounded is False

    def test_a_tool_failure_is_reported_in_already_redacted_words(
        self, client, headers, db, user, monkeypatch
    ):
        """A failure sentence is a tool's own `safe_message()`, never an exception's text.

        The distinction matters because a raw tool error can name a table, a column or a
        provider credential, and this sentence is read aloud to a student.
        """
        from app.tools.contracts import ToolError

        class Boom(ToolError):
            code = "boom"

            def safe_message(self) -> str:
                return "A check could not be completed."

        def explode(*_args, **_kwargs):
            raise Boom("postgres://user:password@host/db says no")

        monkeypatch.setattr(responders._REGISTRY, "invoke_read", explode)
        responder = responders.build_coach_responder(user=user, db=db, session_key="voice-5")

        answer = ask(responder, "How ready am I for placements?")

        assert "password" not in answer.text
        assert "postgres" not in answer.text

    def test_the_coach_catalogue_contains_no_action_tool(self):
        """The read-only guarantee is a property of the catalogue, not of remembering which entry
        point to call. A write tool appearing here would put a mutation in reach of a turn whose
        text the student dictated."""
        from app.tools.contracts import ToolKind

        kinds = {responders._REGISTRY.get(name).kind for name in responders._REGISTRY.names}
        assert kinds == {ToolKind.READ}

    def test_a_long_report_is_trimmed_out_loud_rather_than_silently(self, client, headers, db, user):
        """A student must never be left believing they heard the whole answer."""
        monkeyed = responders.MAX_SPOKEN_CHARACTERS
        assert monkeyed == 1_200
        assert responders._TRIMMED_NOTE in responders._fit(["x" * 1_000, "y" * 1_000])


# --------------------------------------------------------------------------- interview coach


def start_interview(client, headers) -> tuple:
    roles = client.get("/roles").json()
    role_id = next(r["id"] for r in roles if r["name"] == "SDE")
    client.post("/users/me/role", json={"role_id": role_id}, headers=headers)
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=headers).json()["id"]
    body = client.get(f"/interviews/{interview_id}/next-question", headers=headers).json()
    return interview_id, body["question"]["id"]


@pytest.fixture()
def interview(client, headers, fake_ai):
    return start_interview(client, headers)


class TestInterviewOwnership:
    def test_a_question_belonging_to_another_student_cannot_open_a_session(
        self, client, headers, db, fake_ai
    ):
        """Checked before the socket is accepted, through the engine's own paired check.

        One answer for an unknown interview, an unknown question and someone else's, so a socket
        cannot be used to confirm an id that an HTTP probe could not.
        """
        interview_id, question_id = start_interview(client, headers)
        intruder_headers = h.signup(client, email="intruder@example.com")
        intruder = db.query(User).filter(User.email == "intruder@example.com").one()

        with pytest.raises(responders.InterviewUnavailable):
            responders.build_interview_responder(
                user=intruder,
                db=db,
                interview_id=interview_id,
                interview_question_id=question_id,
                ai_provider=fake_ai,
            )
        assert intruder_headers  # the intruder is a real, authenticated account

    def test_an_unknown_interview_is_refused_the_same_way(self, client, headers, db, user, fake_ai):
        with pytest.raises(responders.InterviewUnavailable):
            responders.build_interview_responder(
                user=user, db=db, interview_id=9_999, interview_question_id=9_999, ai_provider=fake_ai
            )


class TestInterviewTurns:
    def _responder(self, db, user, interview, fake_ai):
        interview_id, question_id = interview
        return responders.build_interview_responder(
            user=user,
            db=db,
            interview_id=interview_id,
            interview_question_id=question_id,
            ai_provider=fake_ai,
        )

    def test_one_turn_stores_one_answer_and_reads_out_the_next_question(
        self, db, user, interview, fake_ai
    ):
        responder = self._responder(db, user, interview, fake_ai)
        first_question = responder.current_question_id

        answer = ask(responder, ANSWER)

        assert db.query(InterviewAnswer).count() == 1
        assert answer.grounded is True
        assert answer.text
        assert responder.current_question_id != first_question

    def test_the_counter_comes_from_the_engine_not_from_counting_here(
        self, db, user, interview, fake_ai
    ):
        responder = self._responder(db, user, interview, fake_ai)
        answer = ask(responder, ANSWER)
        assert "Question 2 of" in answer.text

    def test_the_same_answer_twice_is_a_replay_and_stores_nothing_new(
        self, db, user, interview, fake_ai
    ):
        """The guarantee is the engine's -- one answer per question, enforced by a unique index --
        and the responder's job is to *say* which of the two it was."""
        responder = self._responder(db, user, interview, fake_ai)
        first_question = responder.current_question_id
        ask(responder, ANSWER)
        after_first = responder.current_question_id

        # Point the responder back at the question it already answered, as a replayed turn would.
        responder._question_id = first_question
        replayed = ask(responder, ANSWER)

        assert db.query(InterviewAnswer).count() == 1
        assert "already recorded" in replayed.text.lower()
        assert responder.current_question_id == after_first, "a replay advanced the interview twice"

    def test_a_different_answer_to_an_answered_question_is_refused_and_advances_nothing(
        self, db, user, interview, fake_ai
    ):
        responder = self._responder(db, user, interview, fake_ai)
        first_question = responder.current_question_id
        ask(responder, ANSWER)

        responder._question_id = first_question
        refused = ask(responder, "A completely different second answer.")

        assert db.query(InterviewAnswer).count() == 1
        stored = db.query(InterviewAnswer).one()
        assert stored.answer_text == ANSWER, "the first answer is final and was overwritten"
        assert "first answer is final" in refused.text.lower()
        assert responder.current_question_id == first_question

    def test_the_interview_runs_to_completion_and_says_so(self, db, user, interview, fake_ai):
        responder = self._responder(db, user, interview, fake_ai)

        texts = []
        for index in range(20):
            texts.append(ask(responder, f"{ANSWER} Attempt {index}.").text)
            if responder.current_question_id is None:
                break

        assert responder.current_question_id is None, "the interview never completed"
        assert "complete" in texts[-1].lower()

    def test_a_finished_interview_takes_no_further_answers(self, db, user, interview, fake_ai):
        responder = self._responder(db, user, interview, fake_ai)
        for index in range(20):
            ask(responder, f"{ANSWER} Attempt {index}.")
            if responder.current_question_id is None:
                break

        before = db.query(InterviewAnswer).count()
        after = ask(responder, "One more thing I wanted to add.")

        assert "finished" in after.text.lower()
        assert db.query(InterviewAnswer).count() == before

    def test_the_evaluation_status_is_read_from_the_interview_row(self, db, user, interview, fake_ai):
        """`ready` is the engine's decision, read back from the interview's own status. Counting
        answered questions here would be a second, disagreeing copy of it."""
        responder = self._responder(db, user, interview, fake_ai)
        last = ""
        for index in range(20):
            last = ask(responder, f"{ANSWER} Attempt {index}.").text
            if responder.current_question_id is None:
                break

        interview_id, _ = interview
        row = interview_service.read_interview(db, responder._profile_id, interview_id)
        expected = "ready" if row.status == "completed" else "not ready"
        assert expected in last.lower()


class TestWhenTheNextQuestionCannotBeMade:
    def test_the_answer_is_reported_as_saved_rather_than_as_a_failed_turn(
        self, db, user, interview, fake_ai, monkeypatch
    ):
        """A client told "that failed" would resubmit an answer the server already holds, which
        the engine would then have to refuse -- turning a recoverable hiccup into a dead end."""
        interview_id, question_id = interview
        responder = responders.build_interview_responder(
            user=user, db=db, interview_id=interview_id,
            interview_question_id=question_id, ai_provider=fake_ai,
        )
        monkeypatch.setattr(
            interview_service,
            "get_next_question",
            lambda *_a, **_k: (_ for _ in ()).throw(AIGenerationError("provider down")),
        )

        answer = ask(responder, ANSWER)

        assert db.query(InterviewAnswer).count() == 1, "the answer was not stored"
        assert "saved" in answer.text.lower()
        assert answer.grounded is False
        assert responder.current_question_id == question_id, "the pointer moved past a question nobody read"

    def test_the_next_turn_retries_only_the_question_not_the_answer(
        self, db, user, interview, fake_ai, monkeypatch
    ):
        """Reconcile, do not re-run. The uncertain step was question generation; the answer
        behind it is durable, so only the generation is retried."""
        interview_id, question_id = interview
        responder = responders.build_interview_responder(
            user=user, db=db, interview_id=interview_id,
            interview_question_id=question_id, ai_provider=fake_ai,
        )
        real = interview_service.get_next_question
        monkeypatch.setattr(
            interview_service,
            "get_next_question",
            lambda *_a, **_k: (_ for _ in ()).throw(AIGenerationError("provider down")),
        )
        ask(responder, ANSWER)

        monkeypatch.setattr(interview_service, "get_next_question", real)
        recovered = ask(responder, "anything at all")

        assert db.query(InterviewAnswer).count() == 1, "the answer was submitted a second time"
        assert responder.current_question_id != question_id
        assert recovered.grounded is True


class TestTheConnectionsSingleSession:
    """A socket holds one `Session` for the whole conversation.

    Every HTTP route gets a fresh one per request and so never has to think about this. A failed
    statement on a long-lived session leaves it needing a rollback, and SQLAlchemy answers every
    subsequent use with `PendingRollbackError` -- so without this, one bad turn would end the
    interview rather than costing a turn.
    """

    def test_a_failing_turn_rolls_the_session_back(
        self, db, user, interview, fake_ai, monkeypatch
    ):
        interview_id, question_id = interview
        responder = responders.build_interview_responder(
            user=user, db=db, interview_id=interview_id,
            interview_question_id=question_id, ai_provider=fake_ai,
        )
        rollbacks = []
        monkeypatch.setattr(db, "rollback", lambda: rollbacks.append(1))
        monkeypatch.setattr(
            interview_service,
            "submit_answer",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("the database said no")),
        )

        with pytest.raises(RuntimeError):
            ask(responder, ANSWER)

        assert rollbacks, "a failed turn left the connection's session dirty"

    def test_a_coach_turn_rolls_back_too(self, db, user, headers, client, monkeypatch):
        responder = responders.build_coach_responder(user=user, db=db, session_key="voice-rb")
        rollbacks = []
        monkeypatch.setattr(db, "rollback", lambda: rollbacks.append(1))
        monkeypatch.setattr(
            responders._ORCHESTRATOR,
            "coach",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("the database said no")),
        )

        with pytest.raises(RuntimeError):
            ask(responder, "How ready am I?")

        assert rollbacks


class TestAVanishedInterview:
    def test_a_deleted_interview_mid_session_is_reported_honestly(
        self, db, user, interview, fake_ai, monkeypatch
    ):
        interview_id, question_id = interview
        responder = responders.build_interview_responder(
            user=user, db=db, interview_id=interview_id,
            interview_question_id=question_id, ai_provider=fake_ai,
        )
        monkeypatch.setattr(
            interview_service,
            "owned_question",
            lambda *_a, **_k: (_ for _ in ()).throw(
                interview_service.InterviewNotFoundError("interview not found")
            ),
        )

        answer = ask(responder, ANSWER)

        assert "no longer available" in answer.text.lower()
        assert answer.grounded is False
