"""Tier 3: against an actual model and an actual speech driver.

Everything else in the speech suite uses fakes. This file is the one that would notice if
faster-whisper changed its array contract, if CTranslate2 stopped accepting int8 on this host,
or if the resampler started handing the recogniser something it cannot read -- none of which a
fake can fail on.

The loop is: synthesise a known sentence with the host TTS driver, run it back through the
real recogniser, and check the words came back. That is a weak accuracy test and a strong
*integration* test, which is the right trade: accuracy against synthetic speech would not
predict accuracy against a student in a noisy hostel room, and claiming otherwise would be
the kind of number this project does not report.

Skipped, with the reason stated, when either half is unavailable:

* `pyttsx3` drives a host speech engine. It works on this Windows host (SAPI5); on Linux it
  needs the espeak-ng OS package, which pip cannot supply -- the still-open question recorded
  in `app/ai/ollama_provider.py` and `tests/unit/test_tts_linux_contract.py`.
* The `faster-whisper` weights must already be present. These tests must never trigger a
  download: an unauthorised several-hundred-megabyte fetch during a test run is not a test
  result, it is a surprise.

A skip here means less evidence, not more.
"""

import math
import time

import numpy as np
import pytest

from app.speech.audio import read_wav, to_recogniser_input, write_wav
from app.speech.contracts import (
    REALTIME_PCM16,
    AudioEncoding,
    AudioFormat,
    CancellationToken,
    SpeechCancelled,
    SynthesizeRequest,
    TranscribeRequest,
)
from app.speech.faster_whisper_stt import FasterWhisperSpeechToText
from app.speech.pyttsx3_tts import Pyttsx3TextToSpeech

SENTENCE = "the quick brown fox jumps over the lazy dog"
MODEL_SIZE = "tiny.en"


def _weights_present(model_size: str) -> bool:
    """True when the weights are already cached, so no test can trigger a download."""
    from pathlib import Path

    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception:
        return False
    # faster-whisper resolves a bare size to the Systran conversion of that checkpoint.
    return (Path(HF_HUB_CACHE) / f"models--Systran--faster-whisper-{model_size}").is_dir()


def _driver_available() -> bool:
    try:
        import pyttsx3

        engine = pyttsx3.init()
    except Exception:
        return False
    return engine is not None


requires_weights = pytest.mark.skipif(
    not _weights_present(MODEL_SIZE),
    reason=f"faster-whisper {MODEL_SIZE} weights are not cached; a test must not download them",
)
requires_driver = pytest.mark.skipif(
    not _driver_available(),
    reason="no host speech driver (pyttsx3 needs SAPI5/NSSpeech/espeak-ng); NEEDS TEST on Linux",
)


@pytest.fixture(scope="module")
def recogniser() -> FasterWhisperSpeechToText:
    return FasterWhisperSpeechToText(model_size=MODEL_SIZE)


@pytest.fixture(scope="module")
def spoken_wav() -> bytes:
    provider = Pyttsx3TextToSpeech()
    return provider.synthesize(SynthesizeRequest(text=SENTENCE)).audio


def words(text: str) -> set[str]:
    return {token.strip(".,!?").lower() for token in text.split() if token.strip(".,!?")}


@requires_driver
class TestRealSynthesis:
    def test_the_driver_produces_a_readable_wav_with_a_plausible_duration(self):
        provider = Pyttsx3TextToSpeech()
        result = provider.synthesize(SynthesizeRequest(text=SENTENCE))

        assert result.audio[:4] == b"RIFF"
        _samples, fmt = read_wav(result.audio)
        assert fmt.encoding is AudioEncoding.WAV
        assert fmt.channel_count == 1
        # A nine-word sentence takes a human a couple of seconds; a driver returning 50 ms has
        # produced a click, and one returning a minute has produced something else entirely.
        assert 1.0 < (result.audio_seconds or 0) < 20.0
        # Measured, not declared: this is the number the report quotes.
        assert result.first_audio_latency_seconds is not None

    def test_the_audio_is_not_silence(self):
        provider = Pyttsx3TextToSpeech()
        audio = provider.synthesize(SynthesizeRequest(text=SENTENCE)).audio
        samples, _fmt = read_wav(audio)
        # The adapter already refuses a header-only file; this catches the subtler case of a
        # driver that writes the right number of zero samples.
        assert float(np.abs(samples).max()) > 0.01


@requires_weights
class TestRealRecognition:
    def test_a_synthetic_tone_transcribes_without_crashing(self, recogniser):
        # No speech in it, so the only assertion worth making is that the pipeline survives:
        # a model fed non-speech must return an empty or near-empty final transcript rather
        # than inventing words.
        t = np.arange(16_000, dtype=np.float32) / 16_000
        tone = (np.sin(2 * math.pi * 440 * t) * 0.3 * 32767).astype("<i2").tobytes()
        result = recogniser.transcribe(TranscribeRequest(tone, REALTIME_PCM16, language="en"))
        assert result.is_final is True
        assert len(result.text) < 200

    def test_the_realtime_pcm_format_is_accepted_by_the_real_model(self, recogniser):
        silence = b"\x00\x00" * 16_000
        result = recogniser.transcribe(TranscribeRequest(silence, REALTIME_PCM16, language="en"))
        assert result.is_final is True
        assert result.audio_seconds == pytest.approx(1.0, abs=0.05)

    def test_a_cancel_mid_request_is_honoured_by_the_real_model(self, recogniser):
        token = CancellationToken()
        token.cancel()
        with pytest.raises(SpeechCancelled):
            recogniser.transcribe(
                TranscribeRequest(b"\x00\x00" * 16_000, REALTIME_PCM16, language="en", cancellation=token)
            )


@requires_weights
@requires_driver
class TestRealLoop:
    def test_synthesised_speech_transcribes_back_to_the_same_words(self, recogniser, spoken_wav):
        result = recogniser.transcribe(
            TranscribeRequest(spoken_wav, AudioFormat(AudioEncoding.WAV, 22_050, 1), language="en")
        )
        recovered = words(result.text)
        expected = words(SENTENCE)
        missing = expected - recovered
        # Every word, because this is clean synthetic speech with no noise: anything less
        # means the conversion path is damaging the signal, which is the failure this test
        # exists to catch.
        assert not missing, f"lost {sorted(missing)} from {result.text!r}"
        assert result.confidence is not None and result.confidence > 0.3
        assert result.is_final is True

    def test_the_pcm_path_and_the_container_path_agree(self, recogniser, spoken_wav):
        """The same audio through both entry points must transcribe the same.

        This is the real point of the test: the container path lets FFmpeg decode and resample,
        while the PCM path converts explicitly through `to_recogniser_input`. If those two
        disagreed, the realtime transport (raw PCM) and the interview upload (a container)
        would be feeding the recogniser different signals -- and only one of them would have
        been tested.
        """
        samples, fmt = read_wav(spoken_wav)
        pcm16 = (np.clip(samples.reshape(-1), -1, 1) * 32767).astype("<i2").tobytes()
        pcm_format = AudioFormat(AudioEncoding.PCM16, fmt.sample_rate_hz, fmt.channel_count)

        via_container = recogniser.transcribe(
            TranscribeRequest(spoken_wav, AudioFormat(AudioEncoding.WAV, fmt.sample_rate_hz, 1), language="en")
        )
        via_pcm = recogniser.transcribe(TranscribeRequest(pcm16, pcm_format, language="en"))

        assert words(via_pcm.text) == words(via_container.text)

    def test_resampling_from_a_device_rate_does_not_lose_words(self, recogniser, spoken_wav):
        """48 kHz capture, which is what a lot of Android hardware actually gives.

        The VA-2 note recorded that the Android resampler is unfit to feed a recogniser. This
        is the check that the backend's replacement is fit: upsample to 48 kHz, hand it in as
        PCM at that rate, and require the words back.
        """
        samples, fmt = read_wav(spoken_wav)
        at_16k = to_recogniser_input(
            (np.clip(samples.reshape(-1), -1, 1) * 32767).astype("<i2").tobytes(),
            AudioFormat(AudioEncoding.PCM16, fmt.sample_rate_hz, 1),
            max_seconds=60,
        )
        # Round-trip through 48 kHz to exercise the resampler in both directions.
        as_48k_wav = write_wav(at_16k, sample_rate_hz=16_000)
        upsampled = to_recogniser_input(
            (np.clip(read_wav(as_48k_wav)[0].reshape(-1), -1, 1) * 32767).astype("<i2").tobytes(),
            AudioFormat(AudioEncoding.PCM16, 16_000, 1),
            max_seconds=60,
            target_rate_hz=48_000,
        )
        pcm48 = (np.clip(upsampled, -1, 1) * 32767).astype("<i2").tobytes()

        result = recogniser.transcribe(
            TranscribeRequest(pcm48, AudioFormat(AudioEncoding.PCM16, 48_000, 1), language="en")
        )
        missing = words(SENTENCE) - words(result.text)
        assert not missing, f"resampling lost {sorted(missing)} from {result.text!r}"

    def test_the_measured_real_time_factor_is_recorded(self, recogniser, spoken_wav):
        """Measures rather than asserts a latency target.

        A pass/fail threshold here would be a claim about *this* machine's CPU under *this*
        load, and the report would then carry a number that means nothing on a server. So the
        bound is only sanity -- slower than real time on a two-second clip would mean
        something is badly wrong -- and the measurement itself is what gets reported.
        """
        request = TranscribeRequest(
            spoken_wav, AudioFormat(AudioEncoding.WAV, 22_050, 1), language="en", budget_seconds=120
        )
        recogniser.transcribe(request)  # warm: excludes the one-time model load

        started = time.perf_counter()
        result = recogniser.transcribe(request)
        elapsed = time.perf_counter() - started

        audio_seconds = result.audio_seconds or 0.0
        assert audio_seconds > 0
        real_time_factor = elapsed / audio_seconds
        print(f"\nmeasured: {audio_seconds:.2f}s audio in {elapsed:.2f}s (RTF {real_time_factor:.2f}) on CPU int8")
        assert real_time_factor < 5.0, "an order of magnitude slower than real time is a broken setup"
