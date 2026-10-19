"""The authenticated history endpoints.

Conversations are created the way the product creates them -- by opening a real voice session --
and turns are then written through the persistence service, because the speech providers default
to `none` here so the gateway itself produces no transcript. That split is deliberate: the writer
is covered by tests/api/test_voice_conversation_persistence.py, and this file is about what a
client can read, filter and delete.

The ownership tests are the ones that matter most. Every route derives the profile from the access
token, so these prove there is no parameter a client can change to reach another student's record.
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.conversation import Conversation
from app.services import conversation_history as history
from app.voice.session_registry import registry
from tests.fixtures import replay_harness as h

URL = "/voice/session?sample_rate=16000&channels=1&locale=en-IN"


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "conversations_api.db")
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
    return h.signup(client, email="owner@example.com")


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _open_session(client, headers) -> None:
    with client.websocket_connect(URL, headers=headers) as ws:
        body = json.loads(ws.receive_text())
        assert body["type"] == "session_accepted"


def _with_turns(db, texts) -> Conversation:
    """The single stored conversation, given some turns to read back."""
    conversation = db.execute(select(Conversation)).scalars().one()
    for index, (role, text) in enumerate(texts, start=1):
        history.append_turn(
            db, conversation=conversation, role=role, text=text, turn_id="t-%d" % index
        )
    db.commit()
    return conversation


class TestListing:
    def test_a_student_with_no_history_gets_an_empty_list(self, client, headers) -> None:
        response = client.get("/conversations", headers=headers)
        assert response.status_code == 200
        assert response.json() == []

    def test_a_voice_session_appears_in_the_history(self, client, headers) -> None:
        _open_session(client, headers)

        response = client.get("/conversations", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["kind"] == "career_coach"
        assert body[0]["status"] in ("completed", "interrupted")

    def test_the_list_can_be_filtered_by_kind(self, client, headers) -> None:
        _open_session(client, headers)

        assert len(client.get(
            "/conversations?kind=career_coach", headers=headers
        ).json()) == 1
        # The same unified endpoint, asked for the other kind.
        assert client.get("/conversations?kind=mock_interview", headers=headers).json() == []

    def test_an_unknown_kind_is_rejected_rather_than_ignored(self, client, headers) -> None:
        # A filter the server does not understand must not quietly return everything.
        assert client.get("/conversations?kind=chatbot", headers=headers).status_code == 422

    def test_the_page_size_is_bounded(self, client, headers) -> None:
        assert client.get("/conversations?limit=10000", headers=headers).status_code == 422

    def test_history_requires_authentication(self, client) -> None:
        assert client.get("/conversations").status_code in (401, 403)


class TestReadingOneConversation:
    def test_the_transcript_comes_back_in_order(self, client, headers, db) -> None:
        _open_session(client, headers)
        conversation = _with_turns(
            db,
            [("user", "How do I prepare?"), ("assistant", "Start with SQL."),
             ("user", "And after that?")],
        )

        response = client.get("/conversations/%d" % conversation.id, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert [t["seq"] for t in body["turns"]] == [1, 2, 3]
        assert [t["role"] for t in body["turns"]] == ["user", "assistant", "user"]
        assert body["turns"][0]["text"] == "How do I prepare?"

    def test_a_conversation_that_does_not_exist_is_a_404(self, client, headers) -> None:
        assert client.get("/conversations/999999", headers=headers).status_code == 404


class TestOwnership:
    def test_another_students_conversation_is_not_readable(
        self, client, headers, db
    ) -> None:
        _open_session(client, headers)
        conversation = db.execute(select(Conversation)).scalars().one()

        intruder = h.signup(client, email="intruder@example.com")
        response = client.get("/conversations/%d" % conversation.id, headers=intruder)
        # 404, not 403: a 403 would confirm that this id exists.
        assert response.status_code == 404

    def test_another_students_history_is_empty(self, client, headers) -> None:
        _open_session(client, headers)

        intruder = h.signup(client, email="intruder2@example.com")
        assert client.get("/conversations", headers=intruder).json() == []

    def test_another_student_cannot_delete_it(self, client, headers, db) -> None:
        _open_session(client, headers)
        conversation = db.execute(select(Conversation)).scalars().one()

        intruder = h.signup(client, email="intruder3@example.com")
        assert client.delete(
            "/conversations/%d" % conversation.id, headers=intruder
        ).status_code == 404
        # Still readable by its real owner.
        assert client.get(
            "/conversations/%d" % conversation.id, headers=headers
        ).status_code == 200


class TestDeletion:
    def test_a_student_can_delete_their_own_conversation(
        self, client, headers, db
    ) -> None:
        _open_session(client, headers)
        conversation = _with_turns(db, [("user", "something private")])

        assert client.delete(
            "/conversations/%d" % conversation.id, headers=headers
        ).status_code == 204
        assert client.get(
            "/conversations/%d" % conversation.id, headers=headers
        ).status_code == 404
        assert client.get("/conversations", headers=headers).json() == []

    def test_deleting_twice_is_a_404_not_a_server_error(
        self, client, headers, db
    ) -> None:
        _open_session(client, headers)
        conversation = db.execute(select(Conversation)).scalars().one()

        assert client.delete(
            "/conversations/%d" % conversation.id, headers=headers
        ).status_code == 204
        assert client.delete(
            "/conversations/%d" % conversation.id, headers=headers
        ).status_code == 404
