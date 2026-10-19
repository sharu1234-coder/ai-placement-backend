"""The gateway actually writes a transcript, and it can be read back out of the database.

tests/unit/test_conversation_history.py proves the persistence layer in isolation. This file
proves the wiring: that opening a real authenticated WebSocket session creates a conversation row,
that closing it records how it ended, and that the row belongs to the student who opened it. Those
are the parts a service-level test cannot reach, and without them "conversations are saved" would
rest on a schema rather than on behaviour.

`STT_PROVIDER` and `TTS_PROVIDER` default to `none` here, exactly as in
tests/api/test_voice_session_ws.py, so a committed turn comes back as a typed failure and produces
no transcript. That makes the last test below meaningful rather than inconvenient: a turn that
recognised nothing must not leave a row claiming the student said something.
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.conversation import Conversation, ConversationTurn
from app.models.profile import StudentProfile
from app.models.user import User
from app.voice.session_registry import registry
from tests.fixtures import replay_harness as h

URL = "/voice/session?sample_rate=16000&channels=1&locale=en-IN"


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "conversation_persistence.db")
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine, tmp_path, monkeypatch):
    made, _ = h.make_client(engine, tmp_path, monkeypatch)
    return made


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset()
    yield
    registry.reset()


@pytest.fixture()
def headers(client):
    return h.signup(client, email="transcript@example.com")


@pytest.fixture()
def read(engine):
    """A session that reads straight from the database the app wrote to."""
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    def _open():
        return maker()

    return _open


def _accept(ws) -> dict:
    body = json.loads(ws.receive_text())
    assert body["type"] == "session_accepted"
    return body


class TestTheGatewayWritesATranscript:
    def test_opening_a_coach_session_creates_one_retrievable_conversation(
        self, client, headers, read
    ) -> None:
        with client.websocket_connect(URL, headers=headers) as ws:
            accepted = _accept(ws)
            session_id = accepted["session_id"]

        db = read()
        stored = db.execute(select(Conversation)).scalars().all()
        assert len(stored) == 1, "a voice session must leave exactly one transcript"
        conversation = stored[0]
        assert conversation.kind == "career_coach"
        # Keyed on the server-minted session id, which is what lets a reconnect find it again.
        assert conversation.session_key == "voice-" + session_id
        assert conversation.interview_id is None
        db.close()

    def test_closing_the_socket_records_how_the_session_ended(
        self, client, headers, read
    ) -> None:
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)

        db = read()
        conversation = db.execute(select(Conversation)).scalars().one()
        # Either is honest for a socket the client closed; what must not happen is staying
        # `active` for ever, which would show the student a conversation still in progress.
        assert conversation.status in ("completed", "interrupted")
        assert conversation.ended_at is not None
        db.close()

    def test_the_transcript_belongs_to_the_student_who_opened_it(
        self, client, headers, read
    ) -> None:
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)

        db = read()
        conversation = db.execute(select(Conversation)).scalars().one()
        user = db.execute(
            select(User).where(User.email == "transcript@example.com")
        ).scalars().one()
        profile = db.execute(
            select(StudentProfile).where(StudentProfile.user_id == user.id)
        ).scalars().one()
        assert conversation.profile_id == profile.id
        db.close()

    def test_two_sessions_leave_two_separate_transcripts(
        self, client, headers, read
    ) -> None:
        # Each connect mints its own session id, so these are genuinely different conversations
        # rather than a reconnect to the same one.
        for _ in range(2):
            with client.websocket_connect(URL, headers=headers) as ws:
                _accept(ws)

        db = read()
        stored = db.execute(select(Conversation)).scalars().all()
        assert len(stored) == 2
        assert len({c.session_key for c in stored}) == 2
        db.close()


class TestNothingIsInvented:
    def test_a_session_with_no_recognised_speech_stores_no_turns(
        self, client, headers, read
    ) -> None:
        """The providers are `none`, so nothing was transcribed and nothing may be recorded.

        A row here would be a sentence attributed to a student who never said it -- and it would
        be counted by any report that says how many questions they answered.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)

        db = read()
        assert db.execute(select(ConversationTurn)).scalars().all() == []
        # The conversation itself still exists: a session happened, it simply has no turns.
        assert len(db.execute(select(Conversation)).scalars().all()) == 1
        db.close()
