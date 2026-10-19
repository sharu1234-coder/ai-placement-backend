"""VA-0.5 item 0.5-4: the server-side TTS availability contract.

Background. `OllamaProvider.synthesize_speech` calls `pyttsx3.init()` with no driverName, so
it takes the platform default: sapi5 on Windows, nsss on macOS, espeak elsewhere. The code is
not Windows-specific -- VA-0 claimed it was and that claim was withdrawn -- but the *host*
decides whether it works, and the Linux espeak driver loads libespeak through ctypes, so the
espeak-ng OS package has to be installed. pip cannot express that, and this repository has no
deployment image in which to declare it.

So there are two separate questions, and only one of them can be answered on this machine:

  * Does an unusable driver fail in a way the app can act on? Answerable anywhere, by
    simulating the failure. That is what the first two tests do, and they run everywhere.
  * Does the espeak driver actually produce a playable WAV on Linux? Only answerable on a
    Linux host with espeak-ng installed. That is the third test. It is SKIPPED on this
    Windows machine and is the test referred to in the VA-0.5 report as NOT YET RUN --
    nothing here should be read as evidence that Linux TTS works.
"""

import io
import sys
import wave

import pytest

from app.ai.ollama_provider import OllamaProvider
from app.ai.provider import AICapability, AICapabilityError, AIGenerationError

SAMPLE_TEXT = "Tell me about a project you are proud of."


def test_a_driver_that_cannot_initialise_is_a_typed_capability_error(monkeypatch):
    """What a missing espeak-ng looks like, simulated.

    pyttsx3 surfaces this as an OSError from ctypes or as its own EngineError depending on
    how the load fails, so the production code cannot match on the type -- it catches broadly
    and re-raises as AICapabilityError. This asserts the re-raise, because the whole point is
    that an operator can tell "install a package" from "the model misbehaved".
    """
    import pyttsx3

    def _fail_to_init(*args, **kwargs):
        raise OSError("libespeak.so.1: cannot open shared object file")

    monkeypatch.setattr(pyttsx3, "init", _fail_to_init)

    with pytest.raises(AICapabilityError) as caught:
        OllamaProvider().synthesize_speech(SAMPLE_TEXT)

    assert caught.value.capability is AICapability.SYNTHESIZE
    assert caught.value.provider == "ollama"
    # Actionable: the reason has to name the underlying cause, not just "failed".
    assert "could not be initialised" in caught.value.reason
    assert "libespeak" in caught.value.reason
    # Still an AIGenerationError, so the interview-audio route's existing handler catches it
    # and the API contract does not change.
    assert isinstance(caught.value, AIGenerationError)


def test_a_driver_that_writes_no_audio_is_a_typed_capability_error(monkeypatch):
    """The subtler half: espeak's save_to_file support is not documented per-driver, so a
    driver that loads, reports success, and writes nothing is a real possibility.

    Returning that file to the client would be the worst outcome -- Android's MediaPlayer
    would fail to prepare it and the student would see a silent, unexplained failure, with
    nothing in the server logs because as far as the server was concerned it worked.
    """
    import pyttsx3

    class SilentEngine:
        def save_to_file(self, text, path):
            # Leaves the temp file exactly as NamedTemporaryFile created it: zero bytes.
            pass

        def runAndWait(self):
            pass

    monkeypatch.setattr(pyttsx3, "init", lambda *a, **k: SilentEngine())

    with pytest.raises(AICapabilityError) as caught:
        OllamaProvider().synthesize_speech(SAMPLE_TEXT)

    assert caught.value.capability is AICapability.SYNTHESIZE
    assert "no usable WAV output" in caught.value.reason


def test_a_driver_that_writes_a_header_with_no_samples_is_also_rejected(monkeypatch):
    """A 44-byte canonical WAV header and nothing else: structurally valid, zero duration."""
    import pyttsx3

    header_only = io.BytesIO()
    with wave.open(header_only, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22_050)
        handle.writeframes(b"")

    class HeaderOnlyEngine:
        def save_to_file(self, text, path):
            with open(path, "wb") as out:
                out.write(header_only.getvalue())

        def runAndWait(self):
            pass

    monkeypatch.setattr(pyttsx3, "init", lambda *a, **k: HeaderOnlyEngine())

    with pytest.raises(AICapabilityError):
        OllamaProvider().synthesize_speech(SAMPLE_TEXT)


@pytest.mark.skipif(
    sys.platform != "linux",
    reason=(
        "The eSpeak path is Linux-only and this host is not Linux. This is the test that "
        "resolves whether server-side TTS works on a deployment host; until it runs on "
        "Linux with espeak-ng installed, Linux TTS is NOT PROVEN."
    ),
)
def test_synthesize_speech_on_linux_produces_a_wav_the_android_player_can_play():
    """The CI test. Requires the espeak-ng OS package -- see README, "Server-side TTS".

    It asserts more than "some bytes came back", because the consumer is a specific one:
    AudioPlayer.kt hands the file to Android MediaPlayer, which needs real PCM WAV with a
    sane sample rate, not merely a file that starts with RIFF.
    """
    try:
        audio = OllamaProvider().synthesize_speech(SAMPLE_TEXT)
    except AICapabilityError as exc:
        pytest.fail(
            "eSpeak synthesis is unavailable on this Linux host. Install the espeak-ng "
            f"system package (apt-get install -y espeak-ng). Reason: {exc.reason}"
        )

    assert audio[:4] == b"RIFF", "not a RIFF/WAV container"

    with wave.open(io.BytesIO(audio), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        frame_rate = handle.getframerate()
        frames = handle.getnframes()

    # Non-zero duration: the specific failure a header-only file would have produced.
    assert frames > 0, "WAV parsed but contains no audio frames"
    duration_seconds = frames / float(frame_rate)
    assert duration_seconds > 0.2, f"implausibly short synthesis ({duration_seconds:.3f}s)"

    # Compatibility with the existing Android playback path (voice/AudioPlayer.kt ->
    # MediaPlayer): mono or stereo, 8- or 16-bit PCM, standard sample rate range.
    assert channels in (1, 2), f"MediaPlayer expects mono/stereo, got {channels} channels"
    assert sample_width in (1, 2), f"expected 8- or 16-bit PCM, got {sample_width * 8}-bit"
    assert 8_000 <= frame_rate <= 48_000, f"unusual sample rate for MediaPlayer: {frame_rate}"
