"""Tier 2: the adapters' own logic, with fake providers underneath.

No model is loaded and no speech driver is started here. That is the point: every failure path
worth testing -- a driver that will not start, one that writes nothing, a recogniser that dies
halfway through a generator, a cancel arriving mid-transcription -- is unreachable from a test
that needs the real thing working. The real-model test is `test_speech_real_model.py`.
"""

import logging
import math

import numpy as np
import pytest

from app.speech.audio import write_wav
from app.speech.contracts import (
    REALTIME_PCM16,
    AudioEncoding,
    AudioFormat,
    CancellationToken,
    MalformedProviderOutput,
    SpeechCancelled,
    SpeechFailed,
    SpeechTimeout,
    SpeechUnavailable,
    SynthesizeRequest,
    TranscribeRequest,
    UnsupportedAudioFormat,
    UnsupportedLanguage,
)
from app.speech.faster_whisper_stt import FasterWhisperSpeechToText
from app.speech.pyttsx3_tts import Pyttsx3TextToSpeech

SPOKEN = "my greatest weakness is that I over-prepare for interviews"


def pcm(seconds: float, rate: int = 16_000) -> bytes:
    return b"\x00\x01" * int(seconds * rate)


class FakeSegment:
    def __init__(self, text, avg_logprob=-0.2):
        self.text = text
        self.avg_logprob = avg_logprob


class FakeWhisper:
    """Stands in for `WhisperModel`. Yields segments lazily, as the real one does."""

    def __init__(self, segments=None, raise_on_call=None, raise_during=None):
        self.segments = segments if segments is not None else [FakeSegment(" hello"), FakeSegment(" world")]
        self.raise_on_call = raise_on_call
        self.raise_during = raise_during
        self.calls = []
        self.on_segment = None

    def transcribe(self, audio, language=None):
        self.calls.append((getattr(audio, "shape", None), language))
        if self.raise_on_call:
            raise self.raise_on_call

        def generate():
            for index, segment in enumerate(self.segments):
                if self.on_segment:
                    self.on_segment(index)
                if self.raise_during is not None and index == self.raise_during:
                    raise RuntimeError("the runtime died mid-utterance")
                yield segment

        return generate(), object()


def stt(**kwargs) -> FasterWhisperSpeechToText:
    kwargs.setdefault("model", FakeWhisper())
    kwargs.setdefault("model_size", "tiny.en")
    return FasterWhisperSpeechToText(**kwargs)


class TestSttCapabilityDeclaration:
    def test_the_declaration_is_consistent_with_the_code(self):
        stt().validate_declaration()

    def test_a_dot_en_model_declares_english_only(self):
        # A property of the weights, not configuration: tiny.en has no multilingual decoder,
        # so Hindi audio would come back as fluent English-shaped nonsense.
        assert stt(model_size="tiny.en").capabilities.languages == frozenset({"en"})

    def test_a_multilingual_model_declares_detection_rather_than_a_language_list(self):
        assert stt(model_size="small").capabilities.languages == frozenset()

    def test_streaming_is_not_claimed(self):
        caps = stt().capabilities
        assert caps.streaming is False and caps.partial_transcripts is False

    def test_the_realtime_pcm_format_is_accepted(self):
        stt().check_supported(TranscribeRequest(pcm(1), REALTIME_PCM16, language="en"))


class TestSttHappyPath:
    def test_segments_are_joined_and_marked_final(self):
        result = stt().transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16, language="en"))
        assert result.text == "hello world"
        assert result.is_final is True
        assert result.audio_seconds == pytest.approx(1.0, abs=0.01)

    def test_silence_transcribes_to_an_empty_final_transcript(self):
        # Not an error. "The student said nothing" is a real outcome the UI must be able to
        # show; raising here would make it indistinguishable from a provider failure.
        result = stt(model=FakeWhisper(segments=[])).transcribe(
            TranscribeRequest(pcm(1), REALTIME_PCM16, language="en")
        )
        assert result.text == "" and result.is_final is True
        assert result.confidence is None

    def test_confidence_comes_from_the_model_and_stays_in_range(self):
        result = stt(model=FakeWhisper(segments=[FakeSegment("hi", -0.1)])).transcribe(
            TranscribeRequest(pcm(1), REALTIME_PCM16)
        )
        assert result.confidence == pytest.approx(math.exp(-0.1), abs=1e-6)

    def test_a_model_without_logprobs_reports_no_confidence_rather_than_a_default(self):
        class NoLogProb:
            text = "hi"

        result = stt(model=FakeWhisper(segments=[NoLogProb()])).transcribe(
            TranscribeRequest(pcm(1), REALTIME_PCM16)
        )
        assert result.confidence is None

    def test_a_48k_stereo_request_is_converted_rather_than_refused(self):
        model = FakeWhisper()
        fmt = AudioFormat(AudioEncoding.PCM16, 48_000, 2)
        stt(model=model).transcribe(TranscribeRequest(b"\x00\x01" * 48_000 * 2, fmt))
        shape, _language = model.calls[0]
        # One mono 16 kHz second, give or take the resampler's filter delay.
        assert abs(shape[0] - 16_000) < 200

    def test_a_wav_request_is_decoded_through_the_container_path(self):
        model = FakeWhisper()
        wav = write_wav(np.zeros(22_050, dtype=np.float32), sample_rate_hz=22_050)
        stt(model=model).transcribe(TranscribeRequest(wav, AudioFormat(AudioEncoding.WAV, 22_050, 1)))
        shape, _ = model.calls[0]
        assert abs(shape[0] - 16_000) < 200


class TestSttFailurePaths:
    def test_an_unloadable_model_is_unavailable_not_failed(self):
        # Nothing was transcribed and the fix is an operator action, so a caller may retry.
        provider = FasterWhisperSpeechToText(model_size="definitely-not-a-model")
        with pytest.raises(SpeechUnavailable) as caught:
            provider.transcribe(TranscribeRequest(pcm(0.1), REALTIME_PCM16, language="en"))
        assert "could not be loaded" in str(caught.value)

    def test_a_recogniser_that_rejects_the_audio_is_a_failure(self):
        provider = stt(model=FakeWhisper(raise_on_call=RuntimeError("bad input tensor")))
        with pytest.raises(SpeechFailed):
            provider.transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16))

    def test_a_runtime_that_dies_midway_is_a_failure_not_a_partial_result(self):
        # Returning the first half as if it were the answer would be a wrong transcript that
        # reads like a complete one.
        provider = stt(model=FakeWhisper(segments=[FakeSegment("a"), FakeSegment("b")], raise_during=1))
        with pytest.raises(SpeechFailed):
            provider.transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16))

    def test_a_segment_without_text_is_a_contract_violation(self):
        class Empty:
            avg_logprob = -0.2

        with pytest.raises(MalformedProviderOutput):
            stt(model=FakeWhisper(segments=[Empty()])).transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16))

    def test_an_unsupported_language_is_refused_before_the_model_is_touched(self):
        model = FakeWhisper()
        with pytest.raises(UnsupportedLanguage):
            stt(model=model).transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16, language="hi"))
        assert model.calls == []

    def test_an_unsupported_rate_is_refused_before_conversion(self):
        with pytest.raises(UnsupportedAudioFormat):
            stt().transcribe(TranscribeRequest(pcm(1), AudioFormat(AudioEncoding.PCM16, 11_025, 1)))

    def test_audio_over_the_ceiling_is_refused(self):
        provider = stt(max_audio_seconds=0.5)
        with pytest.raises(UnsupportedAudioFormat):
            provider.transcribe(TranscribeRequest(pcm(2), REALTIME_PCM16))


class TestSttCancellationAndTimeout:
    def test_a_cancel_before_the_call_stops_it_immediately(self):
        model = FakeWhisper()
        token = CancellationToken()
        token.cancel()
        with pytest.raises(SpeechCancelled):
            stt(model=model).transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16, cancellation=token))
        assert model.calls == [], "no work should have started"

    def test_a_cancel_during_transcription_is_honoured_at_the_segment_boundary(self):
        # The generator is where inference actually happens, so this is the only point at
        # which cancelling is honest -- and it is observable.
        token = CancellationToken()
        model = FakeWhisper(segments=[FakeSegment("one"), FakeSegment("two"), FakeSegment("three")])
        seen = []

        def cancel_after_first(index):
            seen.append(index)
            if index == 1:
                token.cancel()

        model.on_segment = cancel_after_first
        with pytest.raises(SpeechCancelled) as caught:
            stt(model=model).transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16, cancellation=token))
        assert caught.value.stage == "transcribing"
        assert seen == [0, 1], "it should stop at the boundary, not run to the end"

    def test_an_exhausted_budget_is_a_timeout_naming_both_numbers(self):
        import time

        model = FakeWhisper(segments=[FakeSegment("one"), FakeSegment("two")])
        model.on_segment = lambda index: time.sleep(0.03)
        with pytest.raises(SpeechTimeout) as caught:
            stt(model=model).transcribe(
                TranscribeRequest(pcm(1), REALTIME_PCM16, budget_seconds=0.01)
            )
        assert caught.value.budget_seconds == 0.01

    def test_a_cancel_takes_precedence_over_a_timeout(self):
        # Both are true at once in a slow, cancelled request. Reporting the cancel is what the
        # caller needs: it asked for this, and a timeout would look like a provider fault.
        #
        # The budget has to outlast audio preparation or the request times out before a
        # segment is ever reached -- which is what the first version of this test actually
        # measured.
        import time

        token = CancellationToken()

        def cancel_then_overrun(index):
            token.cancel()
            time.sleep(0.3)

        model = FakeWhisper(segments=[FakeSegment("one"), FakeSegment("two")])
        model.on_segment = cancel_then_overrun
        with pytest.raises(SpeechCancelled):
            stt(model=model).transcribe(
                TranscribeRequest(pcm(1), REALTIME_PCM16, budget_seconds=0.2, cancellation=token)
            )


class TestSttPrivacy:
    def test_no_transcript_and_no_audio_reaches_the_logs(self, caplog):
        # This runs on every utterance. Application logs must not accumulate what a student
        # said about their weaknesses, and a byte count is all an operator needs.
        with caplog.at_level(logging.DEBUG, logger="app.speech"):
            result = stt(model=FakeWhisper(segments=[FakeSegment(f" {SPOKEN}")])).transcribe(
                TranscribeRequest(pcm(1), REALTIME_PCM16, language="en")
            )
        assert result.text == SPOKEN
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "weakness" not in logged
        for fragment in SPOKEN.split():
            assert fragment not in logged.split()
        assert "transcribing" in logged, "the operator-facing line should still be there"


class FakeEngine:
    """Stands in for a pyttsx3 engine."""

    def __init__(self, payload=None, fail_on_write=None):
        self.payload = payload
        self.fail_on_write = fail_on_write
        self.written = []

    def save_to_file(self, text, path):
        self.written.append(text)
        if self.fail_on_write:
            raise self.fail_on_write
        if self.payload is not None:
            with open(path, "wb") as handle:
                handle.write(self.payload)

    def runAndWait(self):  # noqa: N802 - pyttsx3's own name
        return None


def good_wav(seconds: float = 0.5, rate: int = 22_050) -> bytes:
    samples = np.sin(np.arange(int(seconds * rate), dtype=np.float32) / rate * 2 * math.pi * 300) * 0.4
    return write_wav(samples, sample_rate_hz=rate)


def tts(engine) -> Pyttsx3TextToSpeech:
    return Pyttsx3TextToSpeech(engine_factory=lambda: engine)


class TestTtsAdapter:
    def test_the_declaration_is_consistent_with_the_code(self):
        tts(FakeEngine(good_wav())).validate_declaration()

    def test_synthesis_returns_audio_with_its_real_format_and_duration(self):
        result = tts(FakeEngine(good_wav(0.5))).synthesize(SynthesizeRequest(text="hello"))
        assert result.audio[:4] == b"RIFF"
        assert result.audio_format == AudioFormat(AudioEncoding.WAV, 22_050, 1)
        assert result.audio_seconds == pytest.approx(0.5, abs=0.01)

    def test_first_audio_latency_is_measured_not_estimated(self):
        result = tts(FakeEngine(good_wav())).synthesize(SynthesizeRequest(text="hello"))
        # For a batch synthesiser the whole call is the time to first audio: nothing is
        # available before it returns.
        assert result.first_audio_latency_seconds is not None
        assert result.first_audio_latency_seconds >= 0

    def test_the_actual_format_is_reported_even_when_it_differs_from_the_declaration(self):
        # The host driver legitimately differs between machines. A caller that trusted the
        # declared rate and resampled would shift the whole stream.
        result = tts(FakeEngine(good_wav(0.5, rate=16_000))).synthesize(SynthesizeRequest(text="hello"))
        assert result.audio_format.sample_rate_hz == 16_000

    def test_a_driver_that_cannot_start_is_unavailable(self):
        provider = Pyttsx3TextToSpeech(engine_factory=lambda: (_ for _ in ()).throw(OSError("libespeak missing")))
        with pytest.raises(SpeechUnavailable) as caught:
            provider.synthesize(SynthesizeRequest(text="hello"))
        # What a missing espeak-ng OS package looks like on Linux, which pip cannot fix.
        assert "could not be initialised" in str(caught.value)

    @pytest.mark.parametrize("payload", [b"", b"RIFF", b"RIFF" + b"\x00" * 40])
    def test_a_driver_that_writes_nothing_usable_is_unavailable_not_silent_audio(self, payload):
        # Observed in practice. Those bytes would reach Android as a file MediaPlayer cannot
        # prepare -- a silent failure the student sees and the server does not.
        with pytest.raises(SpeechUnavailable) as caught:
            tts(FakeEngine(payload)).synthesize(SynthesizeRequest(text="hello"))
        assert "no usable WAV" in str(caught.value)

    def test_a_driver_that_writes_no_file_at_all_is_a_failure(self):
        with pytest.raises(SpeechUnavailable):
            tts(FakeEngine(None)).synthesize(SynthesizeRequest(text="hello"))

    def test_a_driver_error_while_writing_is_a_failure(self):
        with pytest.raises(SpeechFailed):
            tts(FakeEngine(good_wav(), fail_on_write=RuntimeError("engine died"))).synthesize(
                SynthesizeRequest(text="hello")
            )

    def test_empty_text_is_refused_before_the_driver_starts(self):
        engine = FakeEngine(good_wav())
        with pytest.raises(MalformedProviderOutput):
            tts(engine).synthesize(SynthesizeRequest(text="  "))
        assert engine.written == []

    def test_an_unsupported_language_is_refused_before_the_driver_starts(self):
        engine = FakeEngine(good_wav())
        with pytest.raises(UnsupportedLanguage):
            tts(engine).synthesize(SynthesizeRequest(text="namaste", language="hi"))
        assert engine.written == []

    def test_a_cancel_before_synthesis_stops_it_immediately(self):
        engine = FakeEngine(good_wav())
        token = CancellationToken()
        token.cancel()
        with pytest.raises(SpeechCancelled):
            tts(engine).synthesize(SynthesizeRequest(text="hello", cancellation=token))
        assert engine.written == []

    def test_a_cancel_during_synthesis_withholds_the_audio(self):
        # runAndWait blocks, so the work is already done. Cancelling still prevents the audio
        # being returned, and the wasted synthesis is cheaper than pretending the blocking
        # call was interruptible.
        token = CancellationToken()
        engine = FakeEngine(good_wav())
        original = engine.save_to_file

        def cancel_then_write(text, path):
            token.cancel()
            original(text, path)

        engine.save_to_file = cancel_then_write
        with pytest.raises(SpeechCancelled) as caught:
            tts(engine).synthesize(SynthesizeRequest(text="hello", cancellation=token))
        assert caught.value.stage == "finishing synthesis"

    def test_streaming_synthesis_is_refused_rather_than_faked_as_one_chunk(self):
        from app.speech.contracts import UnsupportedOperation

        with pytest.raises(UnsupportedOperation):
            list(tts(FakeEngine(good_wav())).stream_synthesize(SynthesizeRequest(text="hello")))

    def test_the_text_does_not_reach_the_logs(self, caplog):
        secret = "my salary expectation is twelve lakhs"
        with caplog.at_level(logging.DEBUG, logger="app.speech"):
            tts(FakeEngine(good_wav())).synthesize(SynthesizeRequest(text=secret))
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "salary" not in logged and "lakhs" not in logged
        assert "characters" in logged
