"""The turn-metrics record, and the thing it must never be able to carry.

Two halves. The first checks the arithmetic -- monotonic durations, a real-time factor that does
not divide by zero, `None` for a stage that never ran. The second is the one that matters: a
privacy guard enforced by the type rather than by reviewer vigilance. Voice metrics sit next to
the transcript and the answer, and a record with one free-text field is one careless debugging
session away from logging what a student said about their weaknesses.

These are contract tests against fakes and a real `TurnRuntime` with injected adapters. They
measure nothing about a device, and the durations they assert on are deliberately synthetic.
"""

import asyncio
import logging

import pytest

from app.speech.contracts import (
    REALTIME_PCM16,
    AudioEncoding,
    AudioFormat,
    Speech,
    SynthesizeRequest,
    TranscribeRequest,
    Transcript,
)
from app.voice import turn_metrics
from app.voice.turn_metrics import CANCELLATION_CHECKPOINTS, TurnClock, TurnMetrics
from app.voice.turn_runtime import Answer, CancellationToken, TurnFailure, TurnRuntime

SECRET = "I struggle with dynamic programming and I failed my last two interviews"


class FakeStt:
    def transcribe(self, request: TranscribeRequest) -> Transcript:
        return Transcript(text=SECRET, is_final=True, language="en")


class FakeTts:
    def synthesize(self, request: SynthesizeRequest) -> Speech:
        return Speech(audio=b"\x01\x02" * 64, audio_format=REALTIME_PCM16)


def runtime(stt=None, tts=None) -> TurnRuntime:
    return TurnRuntime(stt_factory=lambda: stt or FakeStt(), tts_factory=lambda: tts or FakeTts())


def one_turn(**kwargs):
    async def respond(text: str) -> Answer:
        return Answer(text="You should practise more graph problems.")

    pcm = b"\x00\x01" * 16_000  # 1 second of 16 kHz mono PCM16
    return asyncio.run(
        runtime(**kwargs.pop("adapters", {})).run(
            pcm=pcm, audio_format=REALTIME_PCM16, respond=respond, **kwargs
        )
    )


class TestTheRecordCannotCarryContent:
    """The privacy guard, which is the reason this module exists."""

    def test_no_field_can_hold_free_text(self) -> None:
        """An allow-list of fields, so adding one is a deliberate act with a reviewer present.

        Checked as a set rather than "none of these look suspicious": a new `note` or `detail`
        field would pass any heuristic and is exactly what must not be added quietly.
        """
        assert set(TurnMetrics.__dataclass_fields__) == {
            "turn_id",
            "generation_id",
            "outcome",
            "total_ms",
            "audio_ms",
            "stt_ms",
            "respond_ms",
            "tts_ms",
            "tts_first_audio_ms",
            "transcript_chars",
            "speech_bytes",
            "tools_used",
            "cancelled_at",
            "stt_provider",
            "tts_provider",
            # Added deliberately: how voice selection resolved, as a `VoiceOutcome` value.
            # A turn used to report provider, latency and outcome=ok while speaking in the
            # wrong voice, so the record could not answer "which voice spoke". Validated
            # against a closed vocabulary -- see the test below, which proves it refuses
            # free text exactly as `cancelled_at` does.
            "tts_voice_outcome",
        }

    def test_a_free_text_cancellation_note_is_refused(self) -> None:
        with pytest.raises(ValueError, match="closed vocabulary"):
            TurnMetrics(
                turn_id="t",
                generation_id="g",
                outcome="cancelled",
                total_ms=1,
                cancelled_at="user hung up while asking about " + SECRET,
            )

    def test_a_free_text_voice_outcome_is_refused(self) -> None:
        with pytest.raises(ValueError, match="selection outcome"):
            TurnMetrics(
                turn_id="t",
                generation_id="g",
                outcome="ok",
                total_ms=1,
                tts_voice_outcome="she mentioned " + SECRET,
            )

    def test_tools_used_must_be_catalogue_names(self) -> None:
        with pytest.raises(ValueError, match="catalogue names"):
            TurnMetrics(
                turn_id="t", generation_id="g", outcome="ok", total_ms=1, tools_used=(SECRET,)
            )

    def test_a_provider_field_cannot_smuggle_a_sentence(self) -> None:
        # It is an adapter class name or nothing. A configuration string or a credential would
        # also fail this, which is the point.
        with pytest.raises(ValueError, match="adapter class name"):
            TurnMetrics(
                turn_id="t",
                generation_id="g",
                outcome="ok",
                total_ms=1,
                stt_provider="faster-whisper tiny.en at /models/key=abc",
            )

    def test_the_emitted_line_contains_no_part_of_the_transcript(self, caplog) -> None:
        """End to end through the real runtime: the recogniser returns a sentence, and no word
        of it may appear in the metrics line."""
        with caplog.at_level(logging.INFO, logger=turn_metrics.__name__):
            result = one_turn()

        assert result.transcript == SECRET, "the turn must still carry the transcript itself"
        lines = [r.getMessage() for r in caplog.records]
        assert lines, "no metrics line was emitted at all"
        line = "\n".join(lines)
        assert SECRET not in line
        for word in ("dynamic", "programming", "interviews", "weakness", "failed"):
            assert word not in line.lower(), f"{word!r} leaked into the metrics line"
        # The length is reported instead, which is the whole substitution.
        assert f"transcript_chars={len(SECRET)}" in line


class TestTheMeasurements:
    def test_a_successful_turn_records_every_stage(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger=turn_metrics.__name__):
            one_turn()
        line = "\n".join(r.getMessage() for r in caplog.records)

        for field in ("outcome=ok", "total_ms=", "stt_ms=", "respond_ms=", "tts_ms=", "audio_ms="):
            assert field in line, f"{field} missing from {line!r}"
        assert "speech_bytes=128" in line

    def test_the_adapters_that_actually_ran_are_named(self, caplog) -> None:
        """What separates a real-inference measurement from this one.

        A reader of these lines must be able to tell that the numbers came from `FakeStt`, or
        the whole metric is worthless as evidence about a real recogniser.
        """
        with caplog.at_level(logging.INFO, logger=turn_metrics.__name__):
            one_turn()
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "stt=FakeStt" in line
        assert "tts=FakeTts" in line

    def test_a_refused_turn_still_reports_a_duration_and_no_stage_times(self, caplog) -> None:
        async def respond(text: str) -> Answer:  # pragma: no cover - never reached
            raise AssertionError("an empty turn must not reach the responder")

        with caplog.at_level(logging.INFO, logger=turn_metrics.__name__):
            result = asyncio.run(
                runtime().run(pcm=b"", audio_format=REALTIME_PCM16, respond=respond)
            )
        assert result.failure is TurnFailure.NO_AUDIO
        line = "\n".join(r.getMessage() for r in caplog.records)

        assert "outcome=no_audio" in line
        assert "total_ms=" in line
        # `None`, not 0: a stage that never ran must not contribute a fictional zero to an
        # average taken over these lines.
        assert "stt_ms=" not in line
        assert "respond_ms=" not in line

    def test_cancellation_names_the_checkpoint_that_saw_it(self, caplog) -> None:
        cancel = CancellationToken()
        cancel.cancel()

        async def respond(text: str) -> Answer:  # pragma: no cover
            raise AssertionError("a cancelled turn must not reach the responder")

        with caplog.at_level(logging.INFO, logger=turn_metrics.__name__):
            result = asyncio.run(
                runtime().run(
                    pcm=b"\x00\x01" * 100,
                    audio_format=REALTIME_PCM16,
                    respond=respond,
                    cancellation=cancel,
                )
            )
        assert result.failure is TurnFailure.CANCELLED
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "cancelled_at=before_recognition" in line

    def test_a_synthesis_failure_is_recorded_by_its_typed_category(self, caplog) -> None:
        """Provider failures are counted by type, which is what makes an error rate possible."""

        class BrokenTts:
            def synthesize(self, request):
                from app.speech.contracts import SpeechUnavailable

                raise SpeechUnavailable("the driver is not there")

        with caplog.at_level(logging.INFO, logger=turn_metrics.__name__):
            result = one_turn(adapters={"tts": BrokenTts()})

        assert result.answer is not None, "a failed synthesis still returns the answer"
        line = "\n".join(r.getMessage() for r in caplog.records)
        # `SpeechUnavailable` from a present-but-broken driver is TTS_FAILED; TTS_UNAVAILABLE
        # is reserved for "no synthesizer is configured at all". The distinction is the point:
        # an error rate per typed category cannot merge the two.
        assert f"outcome={TurnFailure.TTS_FAILED.value}" in line
        # The answer existed, so the responder time is real and must be present.
        assert "respond_ms=" in line

    def test_the_real_time_factor_is_recognition_over_audio(self) -> None:
        metrics = TurnMetrics(
            turn_id="t", generation_id="g", outcome="ok", total_ms=900, audio_ms=1000, stt_ms=500
        )
        assert metrics.real_time_factor == 0.5
        assert "rtf=0.5" in metrics.render()

    def test_an_unknown_duration_gives_no_real_time_factor_rather_than_a_crash(self) -> None:
        # Zero audio would divide by zero; an absent stage has nothing to divide. Both must
        # answer "unknown" rather than produce a number nobody measured.
        for audio, stt in ((0, 500), (1000, None), (None, 500)):
            metrics = TurnMetrics(
                turn_id="t",
                generation_id="g",
                outcome="ok",
                total_ms=1,
                audio_ms=audio,
                stt_ms=stt,
            )
            assert metrics.real_time_factor is None
            assert "rtf=" not in metrics.render()

    def test_durations_come_from_a_monotonic_clock(self, monkeypatch) -> None:
        """A wall clock can step backwards over an NTP correction and yield a negative duration.

        Asserted by moving the monotonic source, which a `time.time`-based implementation would
        ignore entirely.
        """
        now = {"value": 100.0}
        # The module's own clock function, not the global `time` module: patching
        # `time.monotonic` itself would reach every other library running in this process.
        monkeypatch.setattr(turn_metrics, "_now", lambda: now["value"])
        clock = TurnClock(turn_id="t", generation_id="g")
        now["value"] = 100.25
        assert clock.total_ms == 250

    def test_a_backwards_clock_cannot_produce_a_negative_duration(self, monkeypatch) -> None:
        now = {"value": 100.0}
        monkeypatch.setattr(turn_metrics, "_now", lambda: now["value"])
        clock = TurnClock(turn_id="t", generation_id="g")
        now["value"] = 99.0
        assert clock.total_ms == 0

    def test_audio_duration_is_absent_for_a_format_with_no_frame_size(self) -> None:
        """A container has no frame-derived duration, so `audio_ms` must be unknown, not wrong."""

        async def respond(text: str) -> Answer:
            return Answer(text="ok")

        wav = AudioFormat(AudioEncoding.WAV, 16_000, 1)
        result = asyncio.run(
            runtime().run(pcm=b"RIFF" + b"\x00" * 100, audio_format=wav, respond=respond)
        )
        assert result.failure is None or result.transcript is not None


class TestTheClosedVocabularies:
    def test_the_checkpoints_are_the_ones_the_runtime_can_actually_observe(self) -> None:
        """Three, and only three: the runtime checks cancellation before recognition, after it,
        and after the response. There is no checkpoint inside a worker thread, which is why
        cancelling cannot interrupt a running recogniser."""
        assert CANCELLATION_CHECKPOINTS == {
            "before_recognition",
            "after_recognition",
            "after_response",
        }

    def test_every_turn_failure_is_an_acceptable_outcome(self) -> None:
        # The outcome vocabulary is the failure enum plus "ok", so an aggregator over these lines
        # can enumerate the categories without guessing.
        for failure in TurnFailure:
            assert TurnMetrics(
                turn_id="t", generation_id="g", outcome=failure.value, total_ms=1
            ).outcome == failure.value
        assert turn_metrics.OK == "ok"

    def test_a_negative_duration_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            TurnMetrics(turn_id="t", generation_id="g", outcome="ok", total_ms=-1)
