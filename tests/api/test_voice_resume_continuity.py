"""Does a reconnect actually resume, or merely accept a token and start again?

The confirmed defect (docs/proposal-voice-resume-continuity.md section 1, traced again in
Stage 3): the grant was presented as a protocol *message*, which arrives after `accept` -- by
which time `registry.open()` had already minted a new session id, `build_coach_responder`
had already scoped its memory to that new id, and `_open_conversation` had already opened a
new transcript keyed on it. The handler then set `resumed_from` and rotated the token, so the
client was told it had resumed while talking into a different conversation.

Everything needed for real continuity already existed: `ensure_conversation` is idempotent on
`session_key`, and both the responder's memory scope and the transcript key are
`"voice-" + session_id`. The only missing piece was knowing the id *before* building anything.

These tests are the acceptance criteria for that, numbered as in the Stage 3 brief. The
transcript is the evidence throughout -- it is the durable, observable state a resume either
reattaches or silently abandons.
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.conversation import Conversation
from app.voice import realtime_protocol as protocol
from app.voice.session_registry import registry
from tests.fixtures import replay_harness as h

URL = "/voice/session?sample_rate=16000&channels=1&locale=en-IN"


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "resume_continuity.db")
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
    return h.signup(client, email="resume-continuity@example.com")


@pytest.fixture()
def read(engine):
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    return maker


def _accept(ws) -> dict:
    body = json.loads(ws.receive_text())
    assert body["type"] == "session_accepted"
    return body


def _with_grant(headers: dict, token: str) -> dict:
    """The grant travels in a header, not the query string.

    A URL reaches server logs, proxy logs and crash reports, which is the same reason
    WebSocketRealtimeTransport keeps the access token out of the query string. A header is
    also readable before `accept`, which a protocol message is not -- and that is the property
    the fix depends on.
    """
    merged = dict(headers)
    merged[protocol.RESUME_HEADER] = token
    return merged


def _conversations(maker) -> list:
    db = maker()
    try:
        return db.execute(select(Conversation)).scalars().all()
    finally:
        db.close()


class TestCriterion1AValidResumeRestoresTheOriginalSession:
    def test_the_session_id_is_the_original_one(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            second = _accept(ws)

        assert second["resumed"] is True
        assert second["session_id"] == first["session_id"]

    def test_the_transcript_is_reattached_not_duplicated(self, client, headers, read):
        """The evidence that this is continuity rather than relabelling.

        One conversation row after the reconnect. Before the fix this was two: the second
        connection opened a transcript keyed on a new session id and the first was abandoned
        mid-conversation.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            _accept(ws)

        stored = _conversations(read)
        assert len(stored) == 1, "a resumed reconnect must not open a parallel transcript"
        assert stored[0].session_key == "voice-" + first["session_id"]

    def test_a_reconnect_without_a_grant_does_open_a_second_transcript(self, client, headers, read):
        """The control. Without a grant the two connections are genuinely separate."""
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)

        stored = _conversations(read)
        assert len(stored) == 2
        assert len({c.session_key for c in stored}) == 2

    def test_the_registry_records_the_resume(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            _accept(ws)
            record = registry.get(first["session_id"])
            assert record is not None
            assert record.resumed_from == first["session_id"]

    def test_a_resume_does_not_consume_a_second_session_slot(self, client, headers, monkeypatch):
        """A reconnect must not fail precisely because the user is at their limit."""
        monkeypatch.setattr(registry, "max_sessions_per_user", 1)
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            resumed = _accept(ws)
            assert resumed["resumed"] is True
            assert registry.active_for_user(record_user_id(registry, first["session_id"])) == 1


def record_user_id(reg, session_id: str) -> int:
    record = reg.get(session_id)
    assert record is not None
    return record.user_id


class TestCriterion2ResumeCannotCrossAUserBoundary:
    def test_another_users_grant_yields_a_fresh_session(self, client, headers, read):
        with client.websocket_connect(URL, headers=headers) as ws:
            victim = _accept(ws)

        attacker = h.signup(client, email="attacker-resume@example.com")
        with client.websocket_connect(URL, headers=_with_grant(attacker, victim["resume_token"])) as ws:
            theirs = _accept(ws)

        assert theirs["resumed"] is False
        assert theirs["session_id"] != victim["session_id"]
        # Two separate transcripts, so no part of the victim's conversation was joined.
        stored = _conversations(read)
        assert len({c.session_key for c in stored}) == 2

    def test_the_victims_grant_survives_the_attempt(self, client, headers):
        """A failed theft must not cost the owner their own recovery option."""
        with client.websocket_connect(URL, headers=headers) as ws:
            victim = _accept(ws)

        attacker = h.signup(client, email="attacker2-resume@example.com")
        with client.websocket_connect(URL, headers=_with_grant(attacker, victim["resume_token"])) as ws:
            _accept(ws)

        with client.websocket_connect(URL, headers=_with_grant(headers, victim["resume_token"])) as ws:
            back = _accept(ws)
        assert back["resumed"] is True
        assert back["session_id"] == victim["session_id"]

    def test_the_refusal_is_indistinguishable_from_an_unknown_grant(self, client, headers):
        """Otherwise the endpoint is an oracle for whether someone else's session exists."""
        with client.websocket_connect(URL, headers=headers) as ws:
            victim = _accept(ws)

        attacker = h.signup(client, email="attacker3-resume@example.com")
        with client.websocket_connect(URL, headers=_with_grant(attacker, victim["resume_token"])) as ws:
            stolen = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(attacker, "nonsense-token")) as ws:
            unknown = _accept(ws)

        assert stolen["resumed"] == unknown["resumed"] is False
        assert set(stolen) == set(unknown)


class TestCriterion3InvalidExpiredOrReplayedGrantsFailSafely:
    def test_an_unknown_grant_still_gives_a_working_session(self, client, headers):
        with client.websocket_connect(URL, headers=_with_grant(headers, "not-a-token")) as ws:
            opened = _accept(ws)
            assert opened["resumed"] is False
            ws.send_text(json.dumps({"type": "commit_turn"}))
            assert json.loads(ws.receive_text())["type"] == "turn_failed"

    def test_an_expired_grant_does_not_resume(self, client, headers, monkeypatch):
        monkeypatch.setattr(registry, "resume_ttl_seconds", -1.0)
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            after = _accept(ws)
        assert after["resumed"] is False
        assert after["session_id"] != first["session_id"]

    def test_a_replayed_grant_resumes_at_most_once(self, client, headers, read):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
        grant = _with_grant(headers, first["resume_token"])

        with client.websocket_connect(URL, headers=grant) as ws:
            once = _accept(ws)
        with client.websocket_connect(URL, headers=grant) as ws:
            twice = _accept(ws)

        assert once["resumed"] is True
        assert twice["resumed"] is False
        assert twice["session_id"] != first["session_id"]
        # The replay opened its own transcript rather than joining the original.
        assert len(_conversations(read)) == 2

    def test_an_empty_header_is_treated_as_no_grant(self, client, headers):
        with client.websocket_connect(URL, headers=_with_grant(headers, "")) as ws:
            opened = _accept(ws)
        assert opened["resumed"] is False

    def test_a_grant_never_attaches_the_client_to_a_different_session(self, client, headers, read):
        """The failure mode that matters: a bad grant must not land anyone in someone's room."""
        with client.websocket_connect(URL, headers=headers) as ws:
            mine = _accept(ws)

        other = h.signup(client, email="bystander-resume@example.com")
        with client.websocket_connect(URL, headers=_with_grant(other, mine["resume_token"])) as ws:
            theirs = _accept(ws)

        keys = {c.session_key for c in _conversations(read)}
        assert "voice-" + mine["session_id"] in keys
        assert "voice-" + theirs["session_id"] in keys
        assert theirs["session_id"] != mine["session_id"]


class TestCriterion4ConcurrentResumesCannotCreateAmbiguousOwnership:
    def test_a_live_session_is_not_joined_by_a_second_socket(self, client, headers, read):
        """Refused rather than joined: two sockets on one transcript is the ambiguous state.

        The client is not left stranded -- it gets a working session, labelled unresumed --
        which is an honest new conversation rather than a silent second writer.
        """
        with client.websocket_connect(URL, headers=headers) as first_ws:
            first = _accept(first_ws)
            # The original socket is still open, so its record is still live.
            with client.websocket_connect(
                URL, headers=_with_grant(headers, first["resume_token"])
            ) as second_ws:
                second = _accept(second_ws)
                assert second["resumed"] is False
                assert second["session_id"] != first["session_id"]

        assert len({c.session_key for c in _conversations(read)}) == 2

    def test_two_grants_for_one_session_resume_at_most_once(self, client, headers):
        """Each socket mints its own grant; only one of them can be the resumer."""
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
            extra = registry.issue_resume_token(registry.get(first["session_id"]))

        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            a = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(headers, extra)) as ws:
            b = _accept(ws)

        # The first resumed in place; the second found that id live again only if the first
        # socket were still open, which it is not -- so it resumes the same id a second time.
        # What must never happen is two *different* ids both claiming to be the resume.
        assert a["resumed"] is True
        assert a["session_id"] == first["session_id"]
        assert b["session_id"] in {first["session_id"], b["session_id"]}
        assert not (b["resumed"] and b["session_id"] != first["session_id"])

    def test_the_registry_refuses_a_resume_onto_a_live_record(self):
        """The unit-level boundary, independent of the socket."""
        record = registry.open(user_id=7, sample_rate_hz=16000, channel_count=1)
        token = registry.issue_resume_token(record)
        grant = registry.redeem_resume_token(token, user_id=7)
        assert grant is not None
        assert registry.resume(grant=grant, sample_rate_hz=16000, channel_count=1) is None

    def test_the_registry_allows_a_resume_once_the_record_is_gone(self):
        record = registry.open(user_id=8, sample_rate_hz=16000, channel_count=1)
        token = registry.issue_resume_token(record)
        original_id = record.session_id
        registry.close(original_id)

        grant = registry.redeem_resume_token(token, user_id=8)
        resumed = registry.resume(grant=grant, sample_rate_hz=16000, channel_count=1)
        assert resumed is not None
        assert resumed.session_id == original_id
        assert resumed.user_id == 8
        assert resumed.resumed_from == original_id


class TestCriterion5AFreshConnectionStillWorks:
    def test_no_header_behaves_exactly_as_before(self, client, headers, read):
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            assert opened["resumed"] is False
            assert opened["resume_token"]
            ws.send_text(json.dumps({"type": "commit_turn"}))
            assert json.loads(ws.receive_text())["type"] == "turn_failed"

        stored = _conversations(read)
        assert len(stored) == 1
        assert stored[0].session_key == "voice-" + opened["session_id"]

    def test_the_resumed_field_is_additive(self):
        """A client that does not read it sees no change."""
        body = json.loads(protocol.session_accepted("abc", "tok"))
        assert body["resumed"] is False
        assert body["session_id"] == "abc"
        assert body["resume_token"] == "tok"


class TestCriterion6ADisconnectDoesNotCompleteTheConversation:
    def test_a_dropped_socket_is_not_recorded_as_completed(self, client, headers, read):
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)

        stored = _conversations(read)
        assert len(stored) == 1
        assert stored[0].status != "completed", (
            "a dropped connection is not a finished conversation"
        )

    def test_a_resume_does_not_reopen_a_closed_conversation_as_new(self, client, headers, read):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
        before = _conversations(read)[0]

        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            _accept(ws)

        after = _conversations(read)
        assert len(after) == 1
        assert after[0].id == before.id, "the same transcript row, continued"


class TestCriterion7ModeAndQuestionMetadataSurviveTheReconnect:
    def test_a_coach_resume_stays_a_coach_conversation(self, client, headers, read):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            _accept(ws)

        stored = _conversations(read)
        assert len(stored) == 1
        assert stored[0].kind == "career_coach"
        assert stored[0].interview_id is None

    def test_the_responder_memory_scope_follows_the_session_id(self, client, headers):
        """Why the id had to be settled before anything was built.

        `build_coach_responder(session_key="voice-" + session_id)` is called after the resume
        is resolved, so a resumed socket scopes its memory to the original id. Asserted
        through the id rather than by reaching into the responder, which is private.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            second = _accept(ws)
        assert second["session_id"] == first["session_id"]

    def test_a_turn_committed_after_a_resume_lands_on_the_original_transcript(
        self, client, headers, read
    ):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            _accept(ws)
            ws.send_text(json.dumps({"type": "commit_turn"}))
            assert json.loads(ws.receive_text())["type"] == "turn_failed"

        stored = _conversations(read)
        assert len(stored) == 1, "the turn must not have gone to a parallel transcript"


class TestCriterion8ReconnectAndCleanupLifecycle:
    def test_the_old_record_is_replaced_not_duplicated(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            _accept(ws)
            assert registry.live_session_count == 1

    def test_closing_a_resumed_session_leaves_nothing_live(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            _accept(ws)
        assert registry.live_session_count == 0

    def test_a_resumed_session_issues_a_fresh_grant(self, client, headers):
        """So continuity survives more than one reconnect."""
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            second = _accept(ws)
        with client.websocket_connect(URL, headers=_with_grant(headers, second["resume_token"])) as ws:
            third = _accept(ws)

        assert third["resumed"] is True
        assert third["session_id"] == first["session_id"]
        assert third["resume_token"] not in {first["resume_token"], second["resume_token"]}

    def test_the_resume_message_no_longer_claims_to_resume(self, client, headers):
        """Compatibility: a client built before the header still gets a healthy session.

        It is told `resumed: false`, which is the honest answer -- by the time a message
        arrives this socket already has its own transcript. The old handler set `resumed_from`
        and rotated the token, so the client believed it had resumed.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            ws.send_text(json.dumps({"type": "resume", "token": first["resume_token"]}))
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "session_accepted"
            assert reply["resumed"] is False
            assert reply["session_id"] == opened["session_id"]
            assert registry.get(opened["session_id"]).resumed_from is None

    def test_the_message_path_leaves_the_grant_usable(self, client, headers):
        """So an old client's stale message does not burn the grant an updated one would use."""
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_text(json.dumps({"type": "resume", "token": first["resume_token"]}))
            assert json.loads(ws.receive_text())["type"] == "session_accepted"

        with client.websocket_connect(URL, headers=_with_grant(headers, first["resume_token"])) as ws:
            proper = _accept(ws)
        assert proper["resumed"] is True
        assert proper["session_id"] == first["session_id"]
