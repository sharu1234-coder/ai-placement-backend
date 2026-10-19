"""Tier 1: the STT/TTS contracts, with no provider involved.

Nothing here loads a model or touches audio hardware. What it proves is the part that must
hold for *every* provider: that an impossible request is refused before any work starts, and
that a provider cannot describe itself inconsistently with its own code.
"""

import time

import pytest

from app.speech.contracts import (
    REALTIME_PCM16,
    AudioEncoding,
    AudioFormat,
    CancellationToken,
    Deadline,
    MalformedProviderOutput,
    Speech,
    SpeechCancelled,
    SpeechTimeout,
    SpeechToText,
    SpeechUnsupported,
    SttCapabilities,
    SynthesizeRequest,
    TextToSpeech,
    TranscribeRequest,
    Transcript,
    TtsCapabilities,
    UnsupportedAudioFormat,
    UnsupportedLanguage,
    UnsupportedOperation,
)


def _stt_caps(**overrides) -> SttCapabilities:
    defaults = dict(
        input_formats=frozenset({AudioEncoding.PCM16, AudioEncoding.M4A}),
        sample_rates_hz=frozenset({16_000}),
        max_channel_count=1,
        languages=frozenset({"en"}),
        max_audio_seconds=10.0,
        supplies_confidence=False,
    )
    defaults.update(overrides)
    return SttCapabilities(**defaults)


class BatchOnlyStt(SpeechToText):
    name = "batch-only"

    def __init__(self, caps=None):
        self._caps = caps or _stt_caps()

    @property
    def capabilities(self):
        return self._caps

    def transcribe(self, request):
        self.check_supported(request)
        return Transcript(text="ok", is_final=True)


class StreamingStt(SpeechToText):
    name = "streaming"

    @property
    def capabilities(self):
        return _stt_caps(streaming=True, partial_transcripts=True)

    def transcribe(self, request):
        return Transcript(text="final", is_final=True)

    def stream_transcribe(self, request):
        yield Transcript(text="fin", is_final=False)
        yield Transcript(text="final", is_final=True)


def pcm(seconds: float, fmt: AudioFormat = REALTIME_PCM16) -> bytes:
    return b"\x00\x00" * int(seconds * fmt.sample_rate_hz) * fmt.channel_count


class TestAudioFormat:
    def test_the_realtime_format_matches_the_transport(self):
        # The one place this layer and app/voice/realtime_protocol.py must agree. Hard-coding
        # 16 kHz mono in both and hoping is how two halves drift.
        from app.voice import realtime_protocol

        assert REALTIME_PCM16.sample_rate_hz == realtime_protocol.DEFAULT_SAMPLE_RATE_HZ
        assert REALTIME_PCM16.channel_count == 1

    @pytest.mark.parametrize(("rate", "channels"), [(0, 1), (-1, 1), (16_000, 0), (16_000, -2)])
    def test_an_impossible_format_cannot_be_constructed(self, rate, channels):
        with pytest.raises(ValueError):
            AudioFormat(AudioEncoding.PCM16, rate, channels)

    def test_duration_counts_channels(self):
        mono = AudioFormat(AudioEncoding.PCM16, 16_000, 1)
        stereo = AudioFormat(AudioEncoding.PCM16, 16_000, 2)
        assert mono.duration_seconds(32_000) == 1.0
        assert stereo.duration_seconds(32_000) == 0.5

    def test_a_container_has_no_fixed_frame_size(self):
        # Asking for one would mean guessing a bitrate, and a guess here produces a duration
        # that looks authoritative.
        container = AudioFormat(AudioEncoding.M4A, 44_100, 1)
        with pytest.raises(ValueError):
            _ = container.bytes_per_frame


class TestRefusalBeforeWork:
    def test_a_supported_request_is_accepted(self):
        BatchOnlyStt().check_supported(TranscribeRequest(pcm(1), REALTIME_PCM16, language="en"))

    def test_an_unsupported_encoding_is_refused(self):
        with pytest.raises(UnsupportedAudioFormat):
            BatchOnlyStt().check_supported(
                TranscribeRequest(b"RIFFxxxx", AudioFormat(AudioEncoding.WAV, 16_000, 1))
            )

    def test_an_unsupported_pcm_rate_is_refused(self):
        with pytest.raises(UnsupportedAudioFormat):
            BatchOnlyStt().check_supported(
                TranscribeRequest(pcm(1), AudioFormat(AudioEncoding.PCM16, 48_000, 1))
            )

    def test_a_container_rate_is_not_checked_because_the_header_carries_it(self):
        # The caller's rate for an M4A is a guess; the file states the truth. Checking the
        # guess would reject valid recordings.
        BatchOnlyStt().check_supported(
            TranscribeRequest(b"\x00\x00\x00\x18ftypM4A ", AudioFormat(AudioEncoding.M4A, 44_100, 1))
        )

    def test_too_many_channels_is_refused(self):
        with pytest.raises(UnsupportedAudioFormat):
            BatchOnlyStt().check_supported(
                TranscribeRequest(pcm(1, AudioFormat(AudioEncoding.PCM16, 16_000, 2)),
                                  AudioFormat(AudioEncoding.PCM16, 16_000, 2))
            )

    def test_an_unsupported_language_is_refused_and_says_what_is_supported(self):
        with pytest.raises(UnsupportedLanguage) as caught:
            BatchOnlyStt().check_supported(TranscribeRequest(pcm(1), REALTIME_PCM16, language="hi"))
        assert "en" in str(caught.value)

    def test_a_provider_that_detects_language_accepts_any(self):
        provider = BatchOnlyStt(_stt_caps(languages=frozenset()))
        provider.check_supported(TranscribeRequest(pcm(1), REALTIME_PCM16, language="hi"))

    def test_empty_audio_is_refused(self):
        with pytest.raises(MalformedProviderOutput):
            BatchOnlyStt().check_supported(TranscribeRequest(b"", REALTIME_PCM16))

    def test_a_half_sample_is_refused_because_it_shifts_the_stream(self):
        with pytest.raises(UnsupportedAudioFormat) as caught:
            BatchOnlyStt().check_supported(TranscribeRequest(b"\x00\x01\x02", REALTIME_PCM16))
        assert "misaligned" in str(caught.value)

    def test_audio_over_the_duration_ceiling_is_refused_not_truncated(self):
        # Truncating would return a transcript that is wrong in a way nothing downstream can
        # detect: it reads as a complete answer that simply ends early.
        with pytest.raises(UnsupportedAudioFormat) as caught:
            BatchOnlyStt().check_supported(TranscribeRequest(pcm(11), REALTIME_PCM16))
        assert "limit" in str(caught.value)


class TestStreamingIsNeverFaked:
    def test_a_batch_provider_refuses_streaming(self):
        with pytest.raises(UnsupportedOperation):
            list(BatchOnlyStt().stream_transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16)))

    def test_a_batch_provider_does_not_claim_streaming(self):
        assert BatchOnlyStt.declares_streaming() is False
        assert BatchOnlyStt().capabilities.streaming is False

    def test_a_streaming_provider_yields_partials_then_one_final(self):
        results = list(StreamingStt().stream_transcribe(TranscribeRequest(pcm(1), REALTIME_PCM16)))
        assert [r.is_final for r in results] == [False, True]

    def test_declaring_streaming_without_implementing_it_is_refused(self):
        # The VA-0.5 bug in its speech form: a provider that claims a capability and raises at
        # the call site, after the request has been accepted and the audio read.
        liar = BatchOnlyStt(_stt_caps(streaming=True))
        with pytest.raises(SpeechUnsupported) as caught:
            liar.validate_declaration()
        assert "stream_transcribe" in str(caught.value)

    def test_claiming_partials_without_streaming_is_refused(self):
        with pytest.raises(SpeechUnsupported):
            BatchOnlyStt(_stt_caps(partial_transcripts=True)).validate_declaration()

    def test_a_consistent_provider_validates(self):
        BatchOnlyStt().validate_declaration()
        StreamingStt().validate_declaration()

    @pytest.mark.parametrize(
        "caps", [_stt_caps(input_formats=frozenset()), _stt_caps(sample_rates_hz=frozenset())]
    )
    def test_a_provider_that_can_accept_nothing_is_refused(self, caps):
        # It would select successfully and then refuse every request -- a misconfiguration
        # that looks like a working deployment until the first utterance.
        with pytest.raises(SpeechUnsupported):
            BatchOnlyStt(caps).validate_declaration()


class SilentTts(TextToSpeech):
    name = "silent"

    def __init__(self, caps=None):
        self._caps = caps or TtsCapabilities(
            output_format=AudioFormat(AudioEncoding.WAV, 22_050, 1),
            languages=frozenset({"en"}),
            max_characters=20,
        )

    @property
    def capabilities(self):
        return self._caps

    def synthesize(self, request):
        self.check_supported(request)
        return Speech(audio=b"RIFF", audio_format=self._caps.output_format)


class TestTtsContract:
    def test_empty_text_is_refused(self):
        with pytest.raises(MalformedProviderOutput):
            SilentTts().check_supported(SynthesizeRequest(text="   "))

    def test_text_over_the_limit_is_refused(self):
        with pytest.raises(UnsupportedOperation):
            SilentTts().check_supported(SynthesizeRequest(text="x" * 21))

    def test_an_unsupported_language_is_refused(self):
        with pytest.raises(UnsupportedLanguage):
            SilentTts().check_supported(SynthesizeRequest(text="hello", language="ta"))

    def test_an_unknown_voice_is_refused_when_voices_are_declared(self):
        caps = TtsCapabilities(
            output_format=AudioFormat(AudioEncoding.WAV, 22_050, 1),
            languages=frozenset({"en"}),
            max_characters=100,
            voices=frozenset({"aria"}),
        )
        with pytest.raises(UnsupportedOperation):
            SilentTts(caps).check_supported(SynthesizeRequest(text="hello", voice="nobody"))

    def test_a_batch_synthesiser_refuses_streaming(self):
        with pytest.raises(UnsupportedOperation):
            list(SilentTts().stream_synthesize(SynthesizeRequest(text="hello")))

    def test_declaring_streaming_synthesis_without_implementing_it_is_refused(self):
        caps = TtsCapabilities(
            output_format=AudioFormat(AudioEncoding.WAV, 22_050, 1),
            languages=frozenset({"en"}),
            max_characters=100,
            streaming=True,
        )
        with pytest.raises(SpeechUnsupported):
            SilentTts(caps).validate_declaration()


class TestResults:
    def test_a_fabricated_confidence_cannot_be_constructed(self):
        for value in (-0.1, 1.1):
            with pytest.raises(ValueError):
                Transcript(text="x", is_final=True, confidence=value)

    def test_absent_confidence_is_none_not_zero(self):
        # Zero would read as "the model is certain this is wrong"; None reads as "no signal".
        assert Transcript(text="x", is_final=True).confidence is None

    def test_an_empty_final_transcript_is_legitimate(self):
        # Silence transcribes to nothing. Treating that as a failure would turn "the student
        # said nothing" into an error the UI has to explain.
        assert Transcript(text="", is_final=True).text == ""


class TestCancellationAndDeadline:
    def test_cancellation_raises_with_the_stage_named(self):
        token = CancellationToken()
        token.cancel()
        with pytest.raises(SpeechCancelled) as caught:
            token.raise_if_cancelled("p", "transcribing")
        assert caught.value.stage == "transcribing"

    def test_an_uncancelled_token_does_nothing(self):
        CancellationToken().raise_if_cancelled("p", "transcribing")

    def test_a_deadline_needs_a_positive_budget(self):
        with pytest.raises(ValueError):
            Deadline(0)

    def test_an_expired_deadline_reports_both_numbers(self):
        deadline = Deadline(0.01)
        time.sleep(0.02)
        with pytest.raises(SpeechTimeout) as caught:
            deadline.raise_if_expired("p")
        assert caught.value.budget_seconds == 0.01
        assert caught.value.elapsed_seconds > 0.01

    def test_a_live_deadline_does_nothing(self):
        Deadline(30).raise_if_expired("p")
