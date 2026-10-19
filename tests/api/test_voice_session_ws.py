"""The authenticated realtime voice transport endpoint (VA-2).

These exercise the real ASGI WebSocket path through the real app: a genuine handshake, real
headers, real close codes. Nothing here is mocked except the database, which is the same
disposable SQLite every other API test uses.

What they do **not** prove is a voice conversation. No speech provider is configured here --
`STT_PROVIDER` and `TTS_PROVIDER` default to `none` -- so a committed turn comes back as a typed
`stt_unavailable` or `no_audio`, which is the honest outcome on a stock server and is asserted
below. The turn path itself, with fake adapters, is covered in
`tests/api/test_voice_realtime_turn.py`.
"""

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api import voice_session as endpoint
from app.database.base import Base
from app.voice.session_registry import registry
from tests.fixtures import replay_harness as h

URL = "/voice/session?sample_rate=16000&channels=1&locale=en-IN"


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "va2_voice_ws.db")
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine, tmp_path, monkeypatch):
    made, _ = h.make_client(engine, tmp_path, monkeypatch)
    return made


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is a process-wide singleton, so one test's sessions would count against
    another's concurrency limit."""
    registry.reset()
    yield
    registry.reset()


@pytest.fixture()
def headers(client):
    return h.signup(client, email="voice-va2@example.com")


def _accept(ws) -> dict:
    """Read the opening session_accepted frame."""
    body = json.loads(ws.receive_text())
    assert body["type"] == "session_accepted"
    return body


class TestAuthentication:
    def test_an_authenticated_client_gets_a_session_and_a_resume_token(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            body = _accept(ws)
            assert body["session_id"]
            # Issued on open, not on close: a socket that dies has no chance to hand one over.
            assert body["resume_token"]

    def test_a_connection_without_a_token_is_refused(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(URL) as ws:
                ws.receive_text()

    def test_a_forged_token_is_refused(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                URL, headers={"Authorization": "Bearer not.a.jwt"}
            ) as ws:
                ws.receive_text()

    def test_a_malformed_authorization_header_is_refused(self, client, headers):
        for value in ["", "Bearer", "Basic abc", headers["Authorization"].split(" ", 1)[1]]:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(URL, headers={"Authorization": value}) as ws:
                    ws.receive_text()

    def test_a_refresh_token_cannot_open_a_voice_session(self, client):
        """The realtime path must honour the same token rules as HTTP.

        This is the bug that extracting `authenticate_access_token` exists to prevent: a
        second, looser implementation that forgot to check the token type would let a
        long-lived refresh token hold a live microphone channel.
        """
        client.post("/auth/signup", json={"email": "refresh@example.com", "password": h.PASSWORD})
        tokens = client.post(
            "/auth/login", json={"email": "refresh@example.com", "password": h.PASSWORD}
        ).json()
        refresh = tokens.get("refresh_token")
        assert refresh, "login no longer returns a refresh token; this test needs updating"

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                URL, headers={"Authorization": f"Bearer {refresh}"}
            ) as ws:
                ws.receive_text()

    def test_a_token_in_the_query_string_does_not_authenticate(self, client, headers):
        """Credentials belong in headers; a URL reaches logs and crash reports."""
        token = headers["Authorization"].split(" ", 1)[1]
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"{URL}&token={token}") as ws:
                ws.receive_text()


class TestNegotiation:
    @pytest.mark.parametrize("query", ["sample_rate=192000", "sample_rate=0", "channels=5"])
    def test_a_format_the_client_itself_would_refuse_is_refused(self, client, headers, query):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/voice/session?{query}", headers=headers) as ws:
                ws.receive_text()
        # 1008 is in the client's non-retryable set: retrying the same bad format loops.
        assert excinfo.value.code == 1008


class TestMessageHandling:
    def test_a_malformed_control_message_ends_the_session(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_text("this is not JSON")
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_text()
        assert excinfo.value.code == 1008

    def test_a_message_type_outside_the_contract_ends_the_session(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_text(json.dumps({"type": "delete_everything"}))
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_text()
        assert excinfo.value.code == 1008

    def test_an_oversized_control_message_is_refused(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_text(json.dumps({"type": "resume", "token": "x" * 20_000}))
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_text()
        assert excinfo.value.code == 1009

    def test_an_oversized_audio_frame_is_refused(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_bytes(b"\x00" * (endpoint.MAX_BINARY_FRAME_BYTES + 2))
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_text()
        assert excinfo.value.code == 1009

    def test_a_half_sample_audio_frame_is_refused(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_bytes(b"\x00\x01\x02")  # not a whole PCM16 sample
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_text()
        assert excinfo.value.code == 1008

    def test_audio_is_accepted_and_counted(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            session_id = _accept(ws)["session_id"]
            for _ in range(5):
                ws.send_bytes(b"\x00\x01" * 160)  # 20 ms at 16 kHz
            ws.send_text(json.dumps({"type": "commit_turn"}))
            record = registry.get(session_id)
            assert record is not None
            assert record.audio_frames >= 1
            assert record.audio_bytes >= 320

    def test_the_session_audio_budget_is_bounded(self, client, headers, monkeypatch):
        monkeypatch.setattr(endpoint, "MAX_SESSION_AUDIO_BYTES", 1_000)
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            # Sent without interleaving a read: the gateway answers nothing per frame, so a
            # receive between sends would block on a reply that never comes.
            with pytest.raises(WebSocketDisconnect) as excinfo:
                for _ in range(20):
                    ws.send_bytes(b"\x00\x01" * 160)
                ws.receive_text()
        assert excinfo.value.code == 1009


class TestGatewayHonesty:
    """The gateway claims only what it has.

    This class was written when the gateway carried audio and reported nothing about it. It now
    runs real turns, so the assertions below have moved from "it says nothing" to "it does not
    say what it cannot know" -- which was always the point. The three that needed no change
    (`cancel_operation`, `interrupt`, `heartbeat`) are the ones where there is still genuinely
    nothing to report.
    """

    def test_committing_nothing_produces_a_typed_refusal_rather_than_a_transcript(
        self, client, headers
    ):
        """A commit with no audio is the clearest possible test of the rule.

        There is nothing to recognise, so the only honest answers are a typed refusal or
        silence. A transcript here would be invented outright -- and this is the shape a client
        with a dead microphone actually produces, so it is not a hypothetical.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            ws.send_text(json.dumps({"type": "commit_turn"}))
            refusal = json.loads(ws.receive_text())

            # The next reply-producing message, so an invented transcript would have to arrive
            # ahead of it.
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            following = json.loads(ws.receive_text())

        assert refusal == {
            "type": "turn_failed",
            "turn_id": refusal["turn_id"],
            "code": "no_audio",
            "message": refusal["message"],
        }
        assert following["type"] == "session_accepted"

    def test_cancelling_never_produces_an_outcome(self, client, headers):
        """The gateway executes no mutation, so it has no outcome to report.

        An `operation_outcome` invented here would be the false success the specification
        forbids. Unknown must stay unknown.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            ws.send_text(json.dumps({"type": "cancel_operation", "operation_id": "op-1"}))
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            assert json.loads(ws.receive_text())["type"] == "session_accepted"

    def test_an_interrupt_is_accepted_without_claiming_anything_was_stopped(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            session_id = _accept(ws)["session_id"]
            ws.send_text(json.dumps({"type": "interrupt", "generation_id": "g-1"}))
            # Nothing was generating, so there is nothing to cancel and no frame comes back for
            # the interrupt itself. The commit that follows *does* reply, and because frames are
            # handled in order that reply proves the interrupt was processed first -- which is
            # what makes the counter read below deterministic rather than a race.
            ws.send_text(json.dumps({"type": "commit_turn"}))
            assert json.loads(ws.receive_text())["type"] == "turn_failed"
            assert registry.get(session_id).interrupts == 1

    def test_a_heartbeat_is_not_answered(self, client, headers):
        """The client has no 'heartbeat_ack' type and fails the session on any type it cannot
        parse, so a reply here would be an outage rather than a courtesy."""
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            ws.send_text(json.dumps({"type": "heartbeat"}))
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            assert json.loads(ws.receive_text())["type"] == "session_accepted"


class TestLifecycle:
    def test_disconnecting_releases_the_session(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            session_id = _accept(ws)["session_id"]
            assert registry.get(session_id) is not None
        # A session left behind would count against the concurrency cap forever and
        # eventually lock the student out of voice entirely.
        assert registry.get(session_id) is None

    def test_concurrent_sessions_are_capped(self, client, headers):
        registry.max_sessions_per_user = 2
        try:
            with client.websocket_connect(URL, headers=headers) as first:
                _accept(first)
                with client.websocket_connect(URL, headers=headers) as second:
                    _accept(second)
                    with pytest.raises(WebSocketDisconnect) as excinfo:
                        with client.websocket_connect(URL, headers=headers) as third:
                            third.receive_text()
            assert excinfo.value.code == 1008
        finally:
            registry.max_sessions_per_user = 3

    def test_a_session_freed_by_disconnect_can_be_replaced(self, client, headers):
        registry.max_sessions_per_user = 1
        try:
            with client.websocket_connect(URL, headers=headers) as ws:
                _accept(ws)
            with client.websocket_connect(URL, headers=headers) as ws:
                _accept(ws)
        finally:
            registry.max_sessions_per_user = 3


class TestResume:
    def test_a_valid_token_resumes_and_rotates(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=headers) as ws:
            second = _accept(ws)
            ws.send_text(json.dumps({"type": "resume", "token": first["resume_token"]}))
            resumed = json.loads(ws.receive_text())

        assert resumed["type"] == "session_accepted"
        assert resumed["session_id"] == second["session_id"]
        # Rotated: a grant is single use, so a captured token buys one attempt at most.
        assert resumed["resume_token"] != first["resume_token"]

    def test_a_token_is_single_use(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            ws.send_text(json.dumps({"type": "resume", "token": first["resume_token"]}))
            assert json.loads(ws.receive_text())["type"] == "session_accepted"
            # Replayed. Ignored, and the session survives.
            ws.send_text(json.dumps({"type": "resume", "token": first["resume_token"]}))
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            assert json.loads(ws.receive_text())["type"] == "session_accepted"

    def test_another_users_token_is_useless(self, client, headers):
        """The owner check is the security boundary, not the token's secrecy."""
        with client.websocket_connect(URL, headers=headers) as ws:
            victim = _accept(ws)

        attacker = h.signup(client, email="attacker-va2@example.com")
        with client.websocket_connect(URL, headers=attacker) as ws:
            mine = _accept(ws)
            ws.send_text(json.dumps({"type": "resume", "token": victim["resume_token"]}))
            # Silently ignored -- indistinguishable from an unknown token, so the endpoint is
            # not an oracle for whether someone else's session exists.
            ws.send_text(json.dumps({"type": "resume", "token": mine["resume_token"]}))
            resumed = json.loads(ws.receive_text())
            assert resumed["session_id"] == mine["session_id"]
            assert registry.get(mine["session_id"]).resumed_from == mine["session_id"]

    def test_an_unknown_token_does_not_kill_a_healthy_session(self, client, headers):
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            ws.send_text(json.dumps({"type": "resume", "token": "nonsense"}))
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            assert json.loads(ws.receive_text())["type"] == "session_accepted"

    def test_an_expired_token_is_refused(self, client, headers, monkeypatch):
        with client.websocket_connect(URL, headers=headers) as ws:
            first = _accept(ws)

        # Only tokens minted *after* this patch are expired by it: a grant's expires_at is
        # computed at issue time (session_registry.issue_resume_token), so `first` -- minted in
        # the connection above -- keeps its full TTL. close() deliberately leaves a grant
        # alive, because outliving the socket is the whole point of resume. An earlier version
        # of this test asserted both tokens were expired, which was wrong about `first`.
        monkeypatch.setattr(registry, "resume_ttl_seconds", -1.0)
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)

            # Minted under the patched TTL, so already expired: ignored, no reply, and the
            # healthy session survives it.
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            ws.send_text(json.dumps({"type": "commit_turn"}))

            # The synchronisation point, and the reason the assertion below is not a race. The
            # expired resume is answered with nothing; the commit *is* answered -- with a typed
            # `no_audio` refusal, since nothing was spoken -- and because frames are handled in
            # order that reply proves the resume above was processed first.
            assert json.loads(ws.receive_text())["type"] == "turn_failed"

            # The second resume is valid, so it replies too. A successful resume keeps the same
            # record and counters, so it cannot mask the count being checked.
            ws.send_text(json.dumps({"type": "resume", "token": first["resume_token"]}))
            resumed = json.loads(ws.receive_text())
            assert resumed["type"] == "session_accepted"
            assert resumed["session_id"] == opened["session_id"]

            record = registry.get(opened["session_id"])
            assert record.turns_committed == 1
            assert record.resumed_from == first["session_id"]
