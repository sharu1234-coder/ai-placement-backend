"""Tier 3 for the gateway: a committed turn through the **real** selected speech adapters.

Every other realtime test injects fakes, which is right for them -- they are about the gateway's
bookkeeping, and a fake makes each case deterministic. But a fake cannot answer the one question
the acceptance conditions actually ask: *does a committed audio turn produce a genuine transcript
via the selected allowed adapter?* A gateway wired to a fake and a gateway wired to nothing look
identical from inside those tests.

So this file does the whole loop with nothing substituted:

1. the real host speech driver synthesises a known sentence,
2. its samples go down a real WebSocket as raw PCM16 frames at the driver's own rate,
3. `commit_turn` runs the real `TurnRuntime`, which resolves the recogniser through the real
   `app.speech.selection.get_speech_to_text()`,
4. the real `faster-whisper tiny.en` transcribes it,
5. the real career-coach responder answers from the authenticated student's own records,
6. the real driver speaks the answer, and the audio comes back as real frames.

**Actual inference and actual synthesis run here.** That is the point, and it is also why this
file is slow and why it is skipped rather than faked when either half is missing.

Skipped, with the reason stated, when:

* the `faster-whisper tiny.en` weights are not already cached -- a test must never trigger a
  several-hundred-megabyte download, which would be a surprise rather than a test result;
* no host speech driver is available. `pyttsx3` uses SAPI5 on this Windows host; on Linux it
  needs the espeak-ng OS package, which pip cannot supply. **Windows success here is not
  evidence that Linux production TTS works** -- that remains NEEDS TEST, as
  `tests/unit/test_tts_linux_contract.py` records.

A skip here means less evidence, not more. The test prints what actually ran so a report cannot
quote it as a pass that was really a skip.

What this still does not prove: no Android client is involved, no device, and no network. The
realtime client package is unmounted in `main/`. This is the server half, end to end, on one
developer host.
"""

import json

import numpy as np
import pytest

from app.core.config import settings
from app.database.base import Base
from app.speech.audio import read_wav
from app.speech.contracts import SynthesizeRequest
from app.voice import realtime_protocol as protocol
from app.voice.session_registry import registry
from tests.fixtures import replay_harness as h

# The two availability probes, reused rather than re-implemented: a second copy could disagree
# about what "available" means, and the whole value of a skip is that its reason is accurate.
from tests.unit.test_speech_real_model import (
    MODEL_SIZE,
    SENTENCE,
    _driver_available,
    _weights_present,
    words,
)

pytestmark = [
    pytest.mark.skipif(
        not _weights_present(MODEL_SIZE),
        reason=f"faster-whisper {MODEL_SIZE} weights are not cached; a test must not download them",
    ),
    pytest.mark.skipif(
        not _driver_available(),
        reason="no host speech driver (pyttsx3 needs SAPI5/NSSpeech/espeak-ng); NEEDS TEST on Linux",
    ),
]


@pytest.fixture(scope="module")
def spoken_pcm() -> tuple:
    """A real utterance, as raw PCM16 at the driver's own sample rate.

    The driver's rate is used as-is rather than resampled in the test, which also means the
    socket is negotiated at a non-default rate -- so the whole path, including the recogniser's
    own resampling, is exercised on a rate no constant in the codebase mentions.
    """
    from app.speech.pyttsx3_tts import Pyttsx3TextToSpeech

    wav = Pyttsx3TextToSpeech().synthesize(SynthesizeRequest(text=SENTENCE)).audio
    samples, fmt = read_wav(wav)
    pcm = (np.clip(samples.reshape(-1), -1, 1) * 32_767).astype("<i2").tobytes()
    return pcm, fmt.sample_rate_hz


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "realtime_real.db")
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
    return h.signup(client, email="realtime-real@example.com")


@pytest.fixture()
def development_speech(monkeypatch):
    """Select the development-only adapters, explicitly, for the duration of one test.

    This is the configuration gate working as designed: the defaults are `none`, and these
    adapters are reachable only by stating them. `validate_for_production()` refuses both, which
    `tests/unit/test_config_validation.py` covers -- so turning them on here cannot turn them on
    anywhere that matters.
    """
    monkeypatch.setattr(settings, "stt_provider", "faster-whisper")
    monkeypatch.setattr(settings, "tts_provider", "pyttsx3")
    return settings


def _send_utterance(ws, pcm: bytes, rate: int) -> None:
    """Stream the utterance the way the client does: small frames, then a commit."""
    frame_bytes = (rate // 50) * 2  # 20 ms of mono PCM16, a whole number of samples
    for start in range(0, len(pcm), frame_bytes):
        ws.send_bytes(pcm[start : start + frame_bytes])
    ws.send_text(json.dumps({"type": "commit_turn"}))


def _drain(ws, expected_last: str, limit: int = 200) -> list:
    """Read until the named control frame arrives. Audio frames are kept as bytes."""
    frames = []
    for _ in range(limit):
        message = ws.receive()
        if message.get("text") is not None:
            body = json.loads(message["text"])
            frames.append(body)
            if body["type"] == expected_last:
                return frames
        else:
            frames.append(message["bytes"])
    raise AssertionError(f"never saw {expected_last}; got {[_name(f) for f in frames]}")


def _name(frame) -> str:
    return frame["type"] if isinstance(frame, dict) else "audio"


class TestARealTurnEndToEnd:
    def test_a_committed_utterance_is_genuinely_transcribed_and_answered_aloud(
        self, client, headers, spoken_pcm, development_speech, capsys
    ):
        """The acceptance condition, with nothing faked on the speech path.

        Asserting *every* word comes back, not a fuzzy overlap: this is clean synthetic speech
        with no noise, so anything less means the transport is damaging the signal -- which is
        exactly the failure a gateway test can catch and a provider test cannot.

        This is not an accuracy measurement. Accuracy against synthetic speech would not predict
        accuracy for a student in a noisy hostel room, and quoting it as if it did is the kind of
        number this project does not report.
        """
        pcm, rate = spoken_pcm
        url = f"/voice/session?sample_rate={rate}&channels=1&locale=en-IN"

        with client.websocket_connect(url, headers=headers) as ws:
            assert json.loads(ws.receive_text())["type"] == "session_accepted"
            _send_utterance(ws, pcm, rate)
            frames = _drain(ws, "generation_done")

        kinds = [_name(f) for f in frames]
        assert kinds[0] == "thinking"
        assert "final_transcript" in kinds
        assert "assistant_message" in kinds
        assert "audio" in kinds, "no synthesized audio reached the outbound path"

        transcript = next(f for f in frames if _name(f) == "final_transcript")
        missing = words(SENTENCE) - words(transcript["text"])
        assert not missing, f"the gateway lost {sorted(missing)} from {transcript['text']!r}"

        answer = next(f for f in frames if _name(f) == "assistant_message")
        assert answer["spoken"] is True
        assert answer["text"].strip()
        assert answer["turn_id"] == transcript["turn_id"]

        # Real audio, carrying the generation it belongs to, reassembling to a non-trivial buffer.
        started = next(f for f in frames if _name(f) == "generation_started")
        audio = b""
        for frame in [f for f in frames if _name(f) == "audio"]:
            generation_id, chunk = protocol.decode_audio(frame)
            assert generation_id == started["generation_id"]
            audio += chunk
        assert len(audio) > 1_000, "the driver returned a click rather than speech"

        # Printed, not merely asserted: a report must be able to say that inference actually ran
        # rather than inferring it from a green tick.
        with capsys.disabled():
            print(
                f"\n[real adapters] faster-whisper {MODEL_SIZE} transcribed {len(pcm)} PCM bytes "
                f"at {rate} Hz -> {transcript['text']!r}; pyttsx3 returned {len(audio)} bytes "
                f"for a {len(answer['text'])}-character answer."
            )

    def test_the_gateway_resolves_the_recogniser_through_the_real_selection_function(
        self, client, headers, spoken_pcm, development_speech
    ):
        """Proves *which* code chose the adapter, not merely that one was chosen.

        The runtime resolves its providers through `app.speech.selection`, so the development
        configuration above is the only reason a recogniser exists at all. Flipping it back to
        the approved default mid-session must therefore produce `stt_unavailable` on the next
        turn -- which it cannot do if anything along the path has cached a provider or reached
        past the selection function.
        """
        pcm, rate = spoken_pcm
        url = f"/voice/session?sample_rate={rate}&channels=1&locale=en-IN"
        short = pcm[: (rate // 50) * 2 * 10]  # 200 ms is enough to commit a real turn

        with client.websocket_connect(url, headers=headers) as ws:
            assert json.loads(ws.receive_text())["type"] == "session_accepted"

            development_speech.stt_provider = "none"
            _send_utterance(ws, short, rate)
            frames = _drain(ws, "turn_failed")

        assert [_name(f) for f in frames] == ["thinking", "turn_failed"]
        assert frames[-1]["code"] == "stt_unavailable"
