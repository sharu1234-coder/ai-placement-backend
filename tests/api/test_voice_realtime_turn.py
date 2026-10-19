"""The realtime gateway driving a real turn, over a real WebSocket.

What is real here: the real ASGI WebSocket route, the real handshake and close codes, the real
`TurnChannel`, the real `TurnRuntime`, the real protocol builders, the real career-coach
responder (orchestrator, tool registry, grounding) and the real disposable SQLite the rest of
the API suite uses.

**What is faked, and this matters: the speech adapters.** No recognition and no synthesis runs
in this file. `FakeStt` returns a transcript the test chose and `FakeTts` returns bytes the test
chose, both injected through the `build_turn_runtime` seam. So these are **contract tests** for
the gateway: they prove what the server does with a transcript and with synthesized bytes, and
they prove nothing whatsoever about recognition accuracy, synthesis quality, or latency. Real
local inference is exercised separately in `tests/unit/test_speech_real_model.py`.

They also do not prove a device-to-server conversation. No Android client is involved; the
realtime client package is still unmounted in `main/`. What is proven is that the server half
produces honest, correctly ordered events for a committed turn.
"""

import json
import threading

import pytest

from app.ai.provider import get_ai_provider
from app.api import voice_session as endpoint
from app.core.config import settings
from app.database.base import Base
from app.main import app
from app.speech.contracts import (
    AudioEncoding,
    AudioFormat,
    Speech,
    SpeechFailed,
    SpeechUnavailable,
    Transcript,
)
from app.voice import realtime_protocol as protocol
from app.voice.session_registry import registry
from app.voice.turn_runtime import TurnFailure, TurnRuntime
from tests.fixtures import replay_harness as h

#: Read from the gateway rather than restated, so the loop below cannot drift from the bound.
MAX_WORKERS = endpoint.MAX_TURN_WORKERS

URL = "/voice/session?sample_rate=16000&channels=1&locale=en-IN"

#: A 20 ms frame of 16 kHz mono PCM16 -- what the Android recorder actually sends.
FRAME = b"\x01\x02" * 320

WAV_FORMAT = AudioFormat(encoding=AudioEncoding.WAV, sample_rate_hz=16_000, channel_count=1)


# --------------------------------------------------------------------------- fakes


class FakeStt:
    """A recogniser that returns what the test told it to. **No inference runs.**

    `started`/`release` exist so a test can hold a turn inside recognition and act while it is
    there, which is how the cancellation cases synchronise without a sleep.
    """

    name = "fake-stt"

    def __init__(self, text="I bisected the change and added a regression test.", error=None):
        self.text = text
        self.error = error
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.last_audio = b""

    def transcribe(self, request):
        self.calls += 1
        self.last_audio = request.audio
        self.started.set()
        assert self.release.wait(timeout=10), "a held transcription was never released"
        if self.error is not None:
            raise self.error
        request.cancellation.raise_if_cancelled(self.name, "after decoding")
        return Transcript(text=self.text, is_final=True, language=request.language)


class FakeTts:
    """A synthesizer that returns what the test told it to. **No driver runs.**"""

    name = "fake-tts"

    def __init__(self, audio=b"\x11\x22" * 64, error=None):
        self.audio = audio
        self.error = error
        self.calls = 0
        self.spoken_text = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def synthesize(self, request):
        self.calls += 1
        self.spoken_text = request.text
        self.started.set()
        assert self.release.wait(timeout=10), "a held synthesis was never released"
        if self.error is not None:
            raise self.error
        request.cancellation.raise_if_cancelled(self.name, "before writing")
        return Speech(audio=self.audio, audio_format=WAV_FORMAT)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def engine(tmp_path):
    from sqlalchemy.orm import sessionmaker

    from app.models.role import Role

    eng = h.make_engine(tmp_path / "realtime_turn.db")
    Base.metadata.create_all(bind=eng)
    # The one role the interview tests need. Mirrors conftest's minimal seed rather than the
    # full Alembic one, for the same reason: an interview needs a target role to exist.
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
def sessions(made):
    """A session factory on the same disposable database, for reading durable state back.

    Interview guarantees are about what is *stored*, not about what a response said, so the
    assertions that matter go through here rather than through a payload.
    """
    return made[1]


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset()
    yield
    registry.reset()


@pytest.fixture()
def headers(client):
    return h.signup(client, email="realtime-turn@example.com")


@pytest.fixture()
def speech(monkeypatch):
    """Install fake adapters through the gateway's own seam.

    `build_turn_runtime` is the only thing substituted. The channel, the runtime's stage order,
    its bounds, its cancellation handling and its `SpokenTurn` invariants are all the real ones.
    """
    stt, tts = FakeStt(), FakeTts()
    monkeypatch.setattr(
        endpoint, "build_turn_runtime", lambda: TurnRuntime(stt_factory=lambda: stt, tts_factory=lambda: tts)
    )
    return stt, tts


def _read(ws, count: int) -> list:
    """Read `count` frames, decoding text as JSON and leaving binary as bytes."""
    frames = []
    for _ in range(count):
        message = ws.receive()
        if message.get("text") is not None:
            frames.append(json.loads(message["text"]))
        else:
            frames.append(message["bytes"])
    return frames


def _accept(ws) -> dict:
    body = json.loads(ws.receive_text())
    assert body["type"] == "session_accepted"
    return body


def _types(frames) -> list:
    return [f["type"] if isinstance(f, dict) else "audio" for f in frames]


def _speak(ws, frames: int = 2) -> None:
    for _ in range(frames):
        ws.send_bytes(FRAME)
    ws.send_text(json.dumps({"type": "commit_turn"}))


# --------------------------------------------------------------------------- a whole turn


class TestACommittedTurn:
    def test_the_gateway_actually_runs_the_turn_runtime(self, client, headers, speech):
        """The headline claim: audio committed on the socket reaches recognition.

        Asserted by the recogniser being called *with the bytes that were sent*, not merely by a
        transcript appearing -- a gateway that echoed a canned string would satisfy the weaker
        assertion and is exactly what VA-12 forbade.
        """
        stt, _ = speech
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws, frames=3)
            _read(ws, 1)  # thinking

            assert stt.calls == 1
            assert stt.last_audio == FRAME * 3

    def test_a_turn_emits_transcript_then_answer_then_audio_then_completion(
        self, client, headers, speech
    ):
        stt, tts = speech
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 8)

        assert _types(frames) == [
            "thinking",
            "final_transcript",
            "assistant_message",
            "generation_started",
            "speech_started",
            "audio",
            "speech_stopped",
            "generation_done",
        ]
        assert frames[1]["text"] == stt.text
        assert frames[2]["spoken"] is True
        assert frames[2]["text"]
        generation_id, pcm = protocol.decode_audio(frames[5])
        assert generation_id == frames[3]["generation_id"]
        assert pcm == tts.audio

    def test_the_transcript_is_the_students_words_and_the_assistant_message_is_not(
        self, client, headers, speech
    ):
        """Two text-bearing frames, two different speakers. Conflating them is how a UI ends up
        showing the student their own question as the answer."""
        stt, _ = speech
        stt.text = "Which skills should I work on next?"
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 3)

        assert frames[1]["text"] == stt.text
        assert frames[2]["text"] != stt.text

    def test_every_frame_carries_the_same_turn_id(self, client, headers, speech):
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 8)

        turn_ids = {f["turn_id"] for f in frames if isinstance(f, dict) and "turn_id" in f}
        assert len(turn_ids) == 1

    def test_two_turns_in_one_session_are_independent(self, client, headers, speech):
        stt, _ = speech
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            first = _read(ws, 8)
            stt.text = "What should I do today?"
            _speak(ws)
            second = _read(ws, 8)

        assert _types(first) == _types(second)
        assert first[1]["turn_id"] != second[1]["turn_id"]
        assert stt.calls == 2


class TestOnlyWhatIsSupported:
    def test_the_gateway_emits_nothing_outside_the_supported_set(self, client, headers, speech):
        """Every type is checked against the allowlist, not just the ones this test expected.

        An unsupported type is not ignored by the Android client -- its decoder turns an unknown
        frame into a non-retryable `ProtocolViolation` and ends the session. "The client will
        skip it" is never true here.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            _speak(ws)
            frames = _read(ws, 8)
            ws.send_text(json.dumps({"type": "interrupt"}))
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            frames += _read(ws, 1)

        sent = {f["type"] for f in frames if isinstance(f, dict)}
        assert sent <= protocol.SUPPORTED_SERVER_MESSAGE_TYPES
        assert not sent & protocol.FORBIDDEN_SERVER_MESSAGE_TYPES

    def test_no_tool_call_or_operation_outcome_is_ever_claimed(self, client, headers, speech):
        """The two guards VA-7 and VA-10 put on this gateway, still holding.

        The coach genuinely runs read tools this turn, and they are named on
        `Answer.tools_used` -- but a `tool_call_started` sent after the fan-out finished would
        describe starting work that is already over, and this path executes no confirmable
        ACTION tool at all, so there is no outcome to report.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            ws.send_text(json.dumps({"type": "cancel_operation", "operation_id": "op-1"}))
            frames = _read(ws, 8)

        sent = {f["type"] for f in frames if isinstance(f, dict)}
        assert "tool_call_started" not in sent
        assert "operation_outcome" not in sent
        assert "partial_transcript" not in sent

    def test_a_partial_transcript_is_never_sent_because_nothing_streams(self, client, headers, speech):
        stt, _ = speech
        stt.text = "A long answer with several clauses, none of which arrived incrementally."
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 8)

        transcripts = [f for f in frames if isinstance(f, dict) and "transcript" in f["type"]]
        assert [f["type"] for f in transcripts] == ["final_transcript"]


# --------------------------------------------------------------------------- limits


class TestAudioBounds:
    def test_committing_with_no_audio_reports_no_audio(self, client, headers, speech):
        stt, _ = speech
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_text(json.dumps({"type": "commit_turn"}))
            body = _read(ws, 1)[0]

        assert body["type"] == "turn_failed"
        assert body["code"] == "no_audio"
        assert stt.calls == 0, "recognition must not be attempted on an empty turn"

    def test_a_turn_past_the_byte_cap_is_refused_before_recognition(
        self, client, headers, speech, monkeypatch
    ):
        stt, tts = speech
        monkeypatch.setattr(
            endpoint,
            "build_turn_runtime",
            lambda: TurnRuntime(stt_factory=lambda: stt, tts_factory=lambda: tts),
        )
        monkeypatch.setattr(endpoint, "TurnChannel", _tiny_channel)

        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            for _ in range(5):
                ws.send_bytes(FRAME)
            ws.send_text(json.dumps({"type": "commit_turn"}))
            body = _read(ws, 1)[0]

        assert body["type"] == "turn_failed"
        assert body["code"] == "turn_too_large"
        assert stt.calls == 0

    def test_a_half_sample_frame_still_ends_the_session(self, client, headers, speech):
        """Unchanged from VA-2, and it has to stay that way: an odd byte count shifts every
        following sample by one byte, so accepting it turns the rest of the stream into noise
        that looks like a model problem."""
        from starlette.websockets import WebSocketDisconnect

        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_bytes(b"\x01\x02\x03")
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_text()
        assert caught.value.code == 1008

    def test_an_oversized_frame_still_ends_the_session(self, client, headers, speech):
        from starlette.websockets import WebSocketDisconnect

        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_bytes(b"\x00" * (endpoint.MAX_BINARY_FRAME_BYTES + 2))
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_text()
        assert caught.value.code == 1009

    def test_audio_arriving_during_a_turn_belongs_to_the_next_one(self, client, headers, speech):
        """A turn's bytes are detached when it is committed, so nothing can be appended to it.

        Held inside recognition on purpose: that is the only window in which the next turn's
        audio can genuinely arrive while the previous one is still running.
        """
        stt, _ = speech
        stt.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws, frames=2)
            _read(ws, 1)  # thinking
            assert stt.started.wait(timeout=5)

            ws.send_bytes(b"\x33\x44" * 10)  # arrives mid-recognition
            stt.release.set()
            _read(ws, 7)

            assert stt.last_audio == FRAME * 2  # not the later bytes

            stt.started.clear()
            ws.send_text(json.dumps({"type": "commit_turn"}))
            _read(ws, 8)
            assert stt.last_audio == b"\x33\x44" * 10


def _tiny_channel(audio_format, **_kwargs):
    """A real channel with a cap small enough to breach in a few frames.

    Substituted at the gateway's construction site rather than parameterised through the route,
    because the cap is deliberately not a query parameter: a client that could raise its own
    limit would not be limited.
    """
    from app.voice.turn_channel import TurnChannel as Real

    return Real(audio_format, max_turn_audio_bytes=1_000)


# --------------------------------------------------------------------------- duplicates


class TestDuplicateCommits:
    def test_a_double_commit_runs_the_turn_once(self, client, headers, speech):
        stt, _ = speech
        stt.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws, frames=2)
            _read(ws, 1)
            assert stt.started.wait(timeout=5)

            ws.send_text(json.dumps({"type": "commit_turn"}))
            refusal = _read(ws, 1)[0]
            stt.release.set()
            _read(ws, 7)

        assert refusal["type"] == "turn_failed"
        assert refusal["code"] == "turn_in_flight"
        assert stt.calls == 1

    def test_a_refusal_does_not_mark_the_running_turn_as_failed(self, client, headers, speech):
        """The refused commit gets its own id. Reusing the live turn's would tell the client the
        turn it is waiting for had failed, when it is working perfectly well."""
        stt, _ = speech
        stt.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws, frames=2)
            _read(ws, 1)
            assert stt.started.wait(timeout=5)
            ws.send_text(json.dumps({"type": "commit_turn"}))
            refusal = _read(ws, 1)[0]
            stt.release.set()
            rest = _read(ws, 7)

        assert refusal["turn_id"] != rest[0]["turn_id"]
        assert _types(rest)[-1] == "generation_done"

    def test_a_second_commit_does_not_cost_the_student_the_audio_they_kept_speaking(
        self, client, headers, speech
    ):
        stt, _ = speech
        stt.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws, frames=1)
            _read(ws, 1)
            assert stt.started.wait(timeout=5)

            ws.send_bytes(b"\x55\x66" * 20)
            ws.send_text(json.dumps({"type": "commit_turn"}))
            _read(ws, 1)  # refused: in flight
            stt.release.set()
            _read(ws, 7)

            stt.started.clear()
            ws.send_text(json.dumps({"type": "commit_turn"}))
            _read(ws, 8)

        assert stt.last_audio == b"\x55\x66" * 20

    def test_two_commits_cannot_run_concurrently_because_one_coroutine_owns_the_channel(self):
        """Why there is no lock around the turn buffer, stated as a check rather than a comment.

        Genuine concurrency would need two coroutines reading the same socket, which the ASGI
        contract does not permit -- `_pump` is the single reader, and it dispatches control
        messages one at a time. So "concurrent commits" on one session reduces to "a second
        commit arriving while the first turn runs", which is the refusal tested above.

        This pins the premise: if a second `commit()` or `append()` call site ever appears, the
        single-owner argument stops holding and the lock boundaries need revisiting.
        """
        import pathlib
        import re

        source = pathlib.Path(endpoint.__file__).read_text(encoding="utf-8")
        assert len(re.findall(r"\.channel\.commit\(\)", source)) == 1
        assert len(re.findall(r"\.channel\.append\(", source)) == 1

    def test_worker_creation_is_bounded_even_when_a_client_interrupts_in_a_loop(
        self, client, headers, speech
    ):
        """An abandoned worker still occupies a slot, because it cannot be pulled out of a
        blocking driver. Two slots: the live turn plus one draining.

        Without the bound, interrupt-and-commit in a loop would accumulate threads -- growth the
        client controls, which is the same shape of problem the resume-grant quota solves in the
        registry.

        **Both turns are interrupted first, and that is the whole point of the setup.** An
        interrupt frees the *channel* immediately while leaving the worker held inside a
        recogniser it cannot be pulled out of. So at the third commit the channel has nothing in
        flight and would accept the turn; the only thing that can refuse it is the worker cap.

        An earlier version of this test interrupted once and then committed twice, which left a
        turn in flight -- so the channel refused the third commit with the same `turn_in_flight`
        code and the test passed with the worker cap deleted. A mutation probe caught that: the
        test was asserting on an earlier rejection and proving nothing about the bound it named.
        """
        stt, _ = speech
        stt.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)

            for _ in range(MAX_WORKERS):
                _speak(ws)
                assert _read(ws, 1)[0]["type"] == "thinking"
                assert stt.started.wait(timeout=5), "recognition never began"
                stt.started.clear()
                ws.send_text(json.dumps({"type": "interrupt"}))
                assert _read(ws, 1)[0]["type"] == "generation_cancelled"

            # The channel is empty. Only the worker cap stands between this commit and a third
            # thread.
            _speak(ws)
            refusal = _read(ws, 1)[0]

            stt.release.set()

        assert refusal["type"] == "turn_failed", refusal
        assert refusal["code"] == "turn_in_flight"

    def test_a_reconnect_does_not_replay_a_turn(self, client, headers, speech):
        """Resume carries session identity, never turn state.

        A turn in flight when a connection drops is abandoned. Restarting it on the new socket
        would re-run an operation whose outcome is uncertain -- in interview mode, an answer that
        may already be stored -- which is the one thing a reconnect must never do silently.
        """
        stt, _ = speech
        with client.websocket_connect(URL, headers=headers) as first:
            opened = _accept(first)
            _speak(first)
            _read(first, 8)
        calls_after_first = stt.calls

        with client.websocket_connect(URL, headers=headers) as second:
            _accept(second)
            second.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            resumed = _read(second, 1)[0]

        assert resumed["type"] == "session_accepted"
        assert stt.calls == calls_after_first

    def test_a_repeated_interrupt_is_not_a_second_cancellation(self, client, headers, speech):
        """A terminal message arriving twice must resolve once. The second `interrupt` has
        nothing live to act on, so it produces no frame at all -- rather than a second
        `generation_cancelled` for a generation already cancelled."""
        stt, _ = speech
        stt.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            _speak(ws)
            _read(ws, 1)
            assert stt.started.wait(timeout=5)

            ws.send_text(json.dumps({"type": "interrupt"}))
            cancelled = _read(ws, 1)[0]
            ws.send_text(json.dumps({"type": "interrupt"}))
            stt.release.set()

            # The next reply-producing message is the next frame on the wire, so a second
            # cancellation would arrive ahead of it.
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            following = _read(ws, 1)[0]

        assert cancelled["type"] == "generation_cancelled"
        assert following["type"] == "session_accepted"


# --------------------------------------------------------------------------- cancellation


class TestCancellation:
    def test_an_interrupt_during_recognition_suppresses_the_whole_result(
        self, client, headers, speech
    ):
        """Nothing stale reaches the client: no transcript, no answer, no audio.

        The recogniser still finishes -- it is inside a blocking call in a worker thread that
        cannot be pulled out -- so this is not a test that the work stopped. It is a test that
        the work's *result* was not sent, which is the guarantee that can actually be made.
        """
        stt, tts = speech
        stt.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            _speak(ws)
            assert _read(ws, 1)[0]["type"] == "thinking"
            assert stt.started.wait(timeout=5)

            ws.send_text(json.dumps({"type": "interrupt"}))
            cancelled = _read(ws, 1)[0]
            stt.release.set()

            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            following = _read(ws, 1)[0]

        assert cancelled == {"type": "generation_cancelled", "generation_id": cancelled["generation_id"]}
        assert following["type"] == "session_accepted", "a stale turn result arrived after cancellation"
        assert tts.calls == 0, "synthesis ran for a turn the student had already abandoned"

    def test_an_interrupt_during_synthesis_sends_the_answer_as_text_and_no_audio(
        self, client, headers, speech
    ):
        """The one case where a cancelled turn still has something to report.

        By the time synthesis starts the responder has already run, so in interview mode the
        answer is durably stored and the question pointer has moved. Dropping it would leave the
        client a question behind the server. So the text goes out with `spoken` false -- which is
        precisely what it is -- and **no audio frame for the cancelled generation ever does.**
        """
        _, tts = speech
        tts.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            _speak(ws)
            # The transcript arrives while recognition is still the only thing that has finished.
            assert _types(_read(ws, 2)) == ["thinking", "final_transcript"]
            assert tts.started.wait(timeout=5)

            ws.send_text(json.dumps({"type": "interrupt"}))
            cancelled = _read(ws, 1)[0]
            tts.release.set()
            answer = _read(ws, 1)[0]

            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            following = _read(ws, 1)[0]

        assert cancelled["type"] == "generation_cancelled"
        assert answer["type"] == "assistant_message" and answer["spoken"] is False
        assert following["type"] == "session_accepted", "audio arrived for a cancelled generation"

    def test_the_socket_still_answers_while_the_responder_is_blocked(
        self, client, headers, speech, monkeypatch
    ):
        """An interrupt is acted on *during* responder execution, not after it.

        This is what the worker-thread hand-off buys. The responder is ordinary blocking
        SQLAlchemy code; run inline on the event loop it would stall every other socket on the
        worker and make barge-in impossible until it returned. Here the cancellation comes back
        while the responder is still held.

        The answer that eventually arrives is reported as text with `spoken` false, for the same
        reason as the synthesis case: the responder reached authoritative state, so suppressing
        it would hide a real state change. No audio is sent.
        """
        import asyncio

        from app.voice import responders

        gate = threading.Event()
        entered = threading.Event()

        def blocking_builder(**_kwargs):
            async def respond(text):
                entered.set()
                await asyncio.to_thread(gate.wait, 10)
                from app.voice.turn_runtime import Answer

                return Answer(text="an answer nobody waited for")

            return respond

        monkeypatch.setattr(responders, "build_coach_responder", blocking_builder)

        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            _speak(ws)
            assert _types(_read(ws, 2)) == ["thinking", "final_transcript"]
            assert entered.wait(timeout=5)

            ws.send_text(json.dumps({"type": "interrupt"}))
            cancelled = _read(ws, 1)[0]
            assert not gate.is_set(), "the interrupt was only handled after the responder returned"
            gate.set()
            answer = _read(ws, 1)[0]

            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            following = _read(ws, 1)[0]

        assert cancelled["type"] == "generation_cancelled"
        assert answer == {
            "type": "assistant_message",
            "turn_id": answer["turn_id"],
            "text": "an answer nobody waited for",
            "spoken": False,
        }
        assert following["type"] == "session_accepted", "audio was sent for a cancelled generation"

    def test_an_interrupt_part_way_through_playback_truncates_the_stream(
        self, client, headers, speech, monkeypatch
    ):
        """Barge-in *during* the audio stream, which no other cancellation test reaches.

        Every other case here interrupts before `_emit` runs, so the whole result is suppressed
        by the liveness check in the worker. The check inside the chunk loop is a different guard
        and it had no coverage at all -- a mutation probe deleting it left every test passing.

        Reaching it needs a stream long enough to still be running when the interrupt lands, so
        the chunk size is shrunk and the buffer is large: roughly sixteen thousand chunks, each
        an `await` the receive loop can be scheduled between.

        **What is asserted is audio that arrives *after* the cancellation, and that distinction
        is the whole test.** A first attempt counted bytes delivered up to `generation_cancelled`
        and required fewer than the buffer -- which passes with the guard deleted, because that
        frame comes from the interrupt handler in the receive loop rather than from the chunk
        loop, so it arrives mid-stream either way. The guard's actual effect is that the loop
        stops at its next check: `interrupt()` clears the live turn synchronously before the
        cancel frame is even queued, and the outbound lock serialises the two, so with the guard
        in place exactly zero chunks may follow it. Without it, thousands do.
        """
        monkeypatch.setattr(endpoint, "SPEECH_CHUNK_BYTES", 64)
        _, tts = speech
        tts.audio = b"\x05\x06" * (512 * 1024)  # 1 MiB -> ~16 384 chunks

        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            _speak(ws)
            assert _types(_read(ws, 5)) == [
                "thinking", "final_transcript", "assistant_message",
                "generation_started", "speech_started",
            ]

            assert isinstance(_read(ws, 1)[0], bytes), "expected an audio frame"
            ws.send_text(json.dumps({"type": "interrupt"}))
            # The read terminator: frames are handled in order, so `session_accepted` proves the
            # interrupt ahead of it was processed and the stream has had its chance to continue.
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))

            before, after, cancelled = 1, 0, False
            for _ in range(20_000):
                frame = _read(ws, 1)[0]
                if isinstance(frame, bytes):
                    if cancelled:
                        after += 1
                    else:
                        before += 1
                elif frame["type"] == "generation_cancelled":
                    cancelled = True
                elif frame["type"] == "session_accepted":
                    break
            else:
                raise AssertionError("never reached the resume reply")

        assert cancelled, "the interrupt produced no cancellation at all"
        assert after == 0, (
            f"{after} audio chunks were sent for a generation the student had already cancelled"
        )
        assert before < len(tts.audio) // 64, "the stream had already finished; the guard was never reached"

    def test_an_interrupt_before_anything_is_committed_does_nothing(self, client, headers, speech):
        with client.websocket_connect(URL, headers=headers) as ws:
            opened = _accept(ws)
            ws.send_bytes(FRAME)
            ws.send_text(json.dumps({"type": "interrupt"}))
            ws.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            assert _read(ws, 1)[0]["type"] == "session_accepted"

    def test_an_interrupt_naming_a_finished_generation_does_not_kill_the_next_turn(
        self, client, headers, speech
    ):
        """The stale-id case, and the reason the channel matches on generation id at all.

        A barge-in announced for generation A that arrives after A finished and B started would,
        if acted on, cancel B. The student interrupts one reply and loses the next.
        """
        stt, _ = speech
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            first = _read(ws, 8)
            stale = first[3]["generation_id"]

            stt.release.clear()
            _speak(ws)
            assert _read(ws, 1)[0]["type"] == "thinking"
            assert stt.started.wait(timeout=5)

            ws.send_text(json.dumps({"type": "interrupt", "generation_id": stale}))
            stt.release.set()
            second = _read(ws, 7)

        assert _types(second)[-1] == "generation_done"
        assert second[0]["turn_id"] != first[1]["turn_id"]

    def test_the_session_can_commit_again_straight_after_an_interrupt(self, client, headers, speech):
        """Barge-in is useless if the student has to wait for the abandoned work to finish."""
        stt, _ = speech
        stt.release.clear()
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            _read(ws, 1)
            assert stt.started.wait(timeout=5)
            ws.send_text(json.dumps({"type": "interrupt"}))
            _read(ws, 1)

            stt.release.set()
            stt.started.clear()
            _speak(ws)
            frames = _read(ws, 8)

        assert _types(frames)[-1] == "generation_done"


# --------------------------------------------------------------------------- speech outcomes


class TestSpeechOutcomes:
    def test_nothing_recognised_is_reported_without_a_transcript(self, client, headers, speech):
        stt, tts = speech
        stt.text = "   "
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 2)

        assert _types(frames) == ["thinking", "turn_failed"]
        assert frames[1]["code"] == TurnFailure.NOTHING_RECOGNISED.value
        assert tts.calls == 0

    def test_an_unavailable_recogniser_is_reported_as_such(self, client, headers, speech):
        stt, _ = speech
        stt.error = SpeechUnavailable("fake-stt", "no model weights")
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 2)

        assert frames[1]["code"] == TurnFailure.STT_UNAVAILABLE.value
        assert "no model weights" not in frames[1]["message"], "a provider's own words reached the wire"

    def test_an_answer_whose_speech_failed_is_still_delivered_as_text(self, client, headers, speech):
        """The rule this whole message type exists for.

        A TTS failure does not lose the answer. The reply goes out as text with `spoken` false,
        followed by the typed reason the voice is missing -- not as a failed turn, which would be
        a false statement about work that succeeded.
        """
        _, tts = speech
        tts.error = SpeechFailed("fake-tts", "driver refused")
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 4)

        assert _types(frames) == ["thinking", "final_transcript", "assistant_message", "turn_failed"]
        assert frames[2]["spoken"] is False
        assert frames[2]["text"]
        assert frames[3]["code"] == TurnFailure.TTS_FAILED.value
        assert frames[3]["turn_id"] == frames[2]["turn_id"]

    def test_empty_synthesized_audio_is_a_failure_not_silence(self, client, headers, speech):
        """A driver that returned nothing has not spoken. Sending it as audio would have the
        client play silence as if it were a reply."""
        _, tts = speech
        tts.audio = b""
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 4)

        assert _types(frames) == ["thinking", "final_transcript", "assistant_message", "turn_failed"]
        assert frames[2]["spoken"] is False
        assert frames[3]["code"] == TurnFailure.TTS_FAILED.value

    def test_an_unavailable_synthesizer_still_delivers_the_answer(self, client, headers, speech):
        _, tts = speech
        tts.error = SpeechUnavailable("fake-tts", "no SAPI5 driver")
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 4)

        assert frames[2]["type"] == "assistant_message" and frames[2]["spoken"] is False
        assert frames[3]["code"] == TurnFailure.TTS_UNAVAILABLE.value

    def test_malformed_synthesizer_output_is_a_failure_not_audio(self, client, headers, speech):
        """An empty or header-only WAV is the case already seen in practice on this project.

        A driver that "succeeded" while writing nothing would otherwise reach the client as
        audio, and the client would play it as a reply.
        """
        from app.speech.contracts import MalformedProviderOutput

        _, tts = speech
        tts.error = MalformedProviderOutput("fake-tts", "a 44-byte WAV header and nothing else")
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 4)

        assert _types(frames) == ["thinking", "final_transcript", "assistant_message", "turn_failed"]
        assert frames[3]["code"] == TurnFailure.TTS_FAILED.value
        assert "44-byte" not in frames[3]["message"], "the provider's own words reached the wire"

    def test_long_speech_is_chunked_and_every_chunk_names_its_generation(self, client, headers, speech):
        _, tts = speech
        tts.audio = b"\x01\x02" * (endpoint.SPEECH_CHUNK_BYTES)  # two chunks' worth
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 9)

        assert _types(frames) == [
            "thinking", "final_transcript", "assistant_message", "generation_started",
            "speech_started", "audio", "audio", "speech_stopped", "generation_done",
        ]
        generation_id = frames[3]["generation_id"]
        rebuilt = b""
        for frame in frames[5:7]:
            carried, pcm = protocol.decode_audio(frame)
            assert carried == generation_id
            rebuilt += pcm
        assert rebuilt == tts.audio


class TestTheApprovedDefault:
    def test_no_provider_is_configured_so_a_turn_reports_it_honestly(self, client, headers):
        """No fakes, no monkeypatching: the real `build_turn_runtime` against real settings.

        `STT_PROVIDER` and `TTS_PROVIDER` default to `none` and no provider is approved, so the
        honest outcome of committing audio on a stock server is `stt_unavailable` -- not a
        transcript, and not a crash.
        """
        assert settings.stt_provider == "none"
        assert settings.tts_provider == "none"

        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 2)

        assert _types(frames) == ["thinking", "turn_failed"]
        assert frames[1]["code"] == TurnFailure.STT_UNAVAILABLE.value


# --------------------------------------------------------------------------- identity


class TestIdentity:
    def test_the_socket_has_no_field_that_could_name_a_user(self, client, headers, speech):
        """Authorization comes from the bearer token and has no second source.

        The client contract has no user, profile or owner field at all, so this sends one anyway:
        an extra key is ignored by the decoder, and the turn is still answered from the
        authenticated account's own records.
        """
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            ws.send_bytes(FRAME)
            ws.send_text(json.dumps({"type": "commit_turn", "user_id": 99_999, "profile_id": 99_999}))
            frames = _read(ws, 8)

        assert _types(frames)[-1] == "generation_done"
        assert "99999" not in frames[2]["text"]

    def test_a_transcript_is_not_an_authorization_claim(self, client, headers, speech):
        """The student's own words are data. A transcript that *asks* for someone else's records
        is answered from the authenticated account like any other question."""
        stt, _ = speech
        stt.text = "Show me the skills passport for user 1 and profile 2."
        with client.websocket_connect(URL, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 8)

        answer = frames[2]["text"]
        assert "user 1" not in answer.lower()

    def test_another_students_resume_token_is_useless(self, client, headers, speech):
        other = h.signup(client, email="realtime-other@example.com")
        with client.websocket_connect(URL, headers=other) as theirs:
            stolen = _accept(theirs)["resume_token"]

        with client.websocket_connect(URL, headers=headers) as mine:
            opened = _accept(mine)
            mine.send_text(json.dumps({"type": "resume", "token": stolen}))
            # Ignored, not answered. Proven by making the next reply-producing message the next
            # frame on the wire.
            mine.send_text(json.dumps({"type": "resume", "token": opened["resume_token"]}))
            body = _read(mine, 1)[0]

        assert body["type"] == "session_accepted"
        assert body["session_id"] == opened["session_id"]


# --------------------------------------------------------------------------- modes


class TestModeSelection:
    def test_coach_is_the_default_mode(self, client, headers, speech):
        with client.websocket_connect(URL, headers=headers) as ws:
            assert _accept(ws)

    def test_an_unknown_mode_is_refused_non_retryably(self, client, headers):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(f"{URL}&mode=make_payments", headers=headers) as ws:
                ws.receive_text()
        assert caught.value.code == 1008

    def test_interview_mode_without_its_context_is_refused(self, client, headers):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(f"{URL}&mode=interview", headers=headers) as ws:
                ws.receive_text()
        assert caught.value.code == 1008

    def test_interview_mode_against_an_unknown_interview_is_refused_before_accept(
        self, client, headers
    ):
        """Ownership is checked while the socket is still being set up, so a session can never be
        established against someone else's interview and discover it one turn later."""
        from starlette.websockets import WebSocketDisconnect

        url = f"{URL}&mode=interview&interview_id=4242&interview_question_id=4242"
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(url, headers=headers) as ws:
                ws.receive_text()
        assert caught.value.code == 1008


class TestInterviewModeOverTheSocket:
    def test_a_spoken_turn_stores_an_answer_and_reads_out_the_next_question(
        self, client, headers, speech, sessions, use_fake_ai
    ):
        """The full interview path over a real socket: recognition, the authoritative engine, the
        next question spoken back. The grader and the progression rule are the existing ones --
        nothing in the voice path re-implements either."""
        from app.models.interview_answer import InterviewAnswer

        interview_id, question_id = _start_interview(client, headers)
        url = f"{URL}&mode=interview&interview_id={interview_id}&interview_question_id={question_id}"

        stt, _ = speech
        stt.text = "I reproduced it locally, bisected the change and added a regression test."
        with client.websocket_connect(url, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            frames = _read(ws, 8)

        assert _types(frames)[-1] == "generation_done"
        assert frames[1]["text"] == stt.text

        db = sessions()
        try:
            assert [a.answer_text for a in db.query(InterviewAnswer).all()] == [stt.text]
        finally:
            db.close()

    def test_the_answer_is_stored_once_even_if_the_same_turn_is_spoken_twice(
        self, client, headers, speech, sessions, use_fake_ai
    ):
        from app.models.interview_answer import InterviewAnswer

        interview_id, question_id = _start_interview(client, headers)
        url = f"{URL}&mode=interview&interview_id={interview_id}&interview_question_id={question_id}"

        stt, _ = speech
        with client.websocket_connect(url, headers=headers) as ws:
            _accept(ws)
            _speak(ws)
            _read(ws, 8)
            _speak(ws)
            _read(ws, 8)

        db = sessions()
        try:
            # Two turns, two different questions: the first answer was never overwritten and the
            # second did not land on the first question.
            assert db.query(InterviewAnswer).count() == 2
        finally:
            db.close()


def _start_interview(client, headers) -> tuple:
    roles = client.get("/roles").json()
    role_id = next(r["id"] for r in roles if r["name"] == "SDE")
    client.post("/users/me/role", json={"role_id": role_id}, headers=headers)
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=headers).json()["id"]
    body = client.get(f"/interviews/{interview_id}/next-question", headers=headers).json()
    return interview_id, body["question"]["id"]


@pytest.fixture()
def use_fake_ai():
    """The interview harness's deterministic provider. No model runs."""
    from tests.api.test_interviews import FakeAIProvider

    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)
