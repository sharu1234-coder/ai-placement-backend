"""The spoken-turn pipeline: recognise, answer, speak -- and never lie about which happened.

Fakes throughout, and they are unmistakable: `FakeStt` and `FakeTts` return fixed values and are
constructed by the test. **No real speech provider runs in this file**, which matters because
the only approved adapters are development-only and `STT_PROVIDER`/`TTS_PROVIDER` both default to
`none`. A passing run here is not evidence that recognition works on any host; that is what
`tests/unit/test_speech_real_model.py` is for, and it skips when the weights or driver are
absent.

The assertions are mostly about what a turn is *not allowed to claim*. A pipeline that reports a
spoken reply it never synthesized, or a transcript it never recognised, would be the exact
failure mode this project keeps finding in its own reports.
"""

import asyncio
import logging

import pytest

from app.speech.contracts import (
    AudioEncoding,
    AudioFormat,
    CancellationToken,
    Speech,
    SpeechFailed,
    SpeechTimeout,
    SpeechUnavailable,
    Transcript,
    UnsupportedLanguage,
)
from app.speech.selection import SpeechNotConfigured
from app.voice.turn_runtime import (
    MAX_TRANSCRIPT_CHARACTERS,
    Answer,
    SpokenTurn,
    TurnFailure,
    TurnRuntime,
    user_message_for,
)

PCM = AudioFormat(encoding=AudioEncoding.PCM16, sample_rate_hz=16_000, channel_count=1)
AUDIO = b"\x00\x01" * 8_000  # a quarter second of 16 kHz mono PCM16


class FakeStt:
    """Unmistakably a fake. Returns what the test told it to, or raises what the test chose."""

    name = "fake-stt"

    def __init__(self, text: str = "I bisected the change and added a test.", raises=None):
        self.text = text
        self.raises = raises
        self.calls = 0

    def transcribe(self, request):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return Transcript(text=self.text, is_final=True)


class FakeTts:
    name = "fake-tts"

    def __init__(self, audio: bytes = b"RIFFfake", raises=None):
        self.audio = audio
        self.raises = raises
        self.calls = 0

    def synthesize(self, request):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return Speech(audio=self.audio, audio_format=PCM)


class VoiceReportingTts(FakeTts):
    """A synthesiser that reports which voice it used, as the real adapter now does.

    Separate from `FakeTts` so the existing tests keep proving that a provider *without*
    `last_voice_choice` still works -- `turn_runtime` reads it with `getattr`, and a provider
    that does not report one must not break a turn.
    """

    def __init__(self, outcome, **kwargs):
        super().__init__(**kwargs)
        from app.core.voice_selection import VoiceChoice

        self.last_voice_choice = VoiceChoice(outcome, "female", voice_name="Zira")


def runtime(stt=None, tts=None, **kwargs) -> TurnRuntime:
    return TurnRuntime(
        stt_factory=lambda: stt if stt is not None else FakeStt(),
        tts_factory=lambda: tts if tts is not None else FakeTts(),
        **kwargs,
    )


async def answerer(text: str) -> Answer:
    return Answer(text=f"You said: {text}")


def run(rt: TurnRuntime, **kwargs):
    kwargs.setdefault("pcm", AUDIO)
    kwargs.setdefault("audio_format", PCM)
    kwargs.setdefault("respond", answerer)
    return asyncio.run(rt.run(**kwargs))


# --------------------------------------------------------------------------- the happy path


class TestACompleteTurn:
    def test_all_three_stages_run_and_the_turn_is_speakable(self):
        stt, tts = FakeStt(), FakeTts()

        turn = run(runtime(stt, tts))

        assert turn.recognised and turn.speakable
        assert turn.failure is None
        assert turn.transcript == "I bisected the change and added a test."
        assert turn.answer is not None and turn.answer.text.startswith("You said:")
        assert stt.calls == 1 and tts.calls == 1

    def test_the_turn_and_generation_ids_are_distinct_and_stable_within_a_turn(self):
        turn = run(runtime())

        assert turn.turn_id.startswith("t-") and turn.generation_id.startswith("g-")
        assert turn.turn_id != turn.generation_id

    def test_a_supplied_turn_id_is_preserved(self):
        turn = run(runtime(), turn_id="t-from-the-client")

        assert turn.turn_id == "t-from-the-client"

    def test_the_responder_receives_the_recognised_text_not_the_audio(self):
        seen = []

        async def capture(text: str) -> Answer:
            seen.append(text)
            return Answer(text="ok")

        run(runtime(FakeStt("my spoken answer")), respond=capture)

        assert seen == ["my spoken answer"]


# --------------------------------------------------------------------------- input bounds


class TestInputBounds:
    def test_no_audio_is_refused_before_anything_runs(self):
        stt = FakeStt()

        turn = run(runtime(stt), pcm=b"")

        assert turn.failure is TurnFailure.NO_AUDIO
        assert stt.calls == 0

    def test_oversized_audio_is_refused_before_recognition(self):
        stt = FakeStt()
        rt = runtime(stt, max_turn_audio_bytes=1_000)

        turn = run(rt, pcm=b"\x00" * 2_000)

        assert turn.failure is TurnFailure.AUDIO_TOO_LARGE
        assert stt.calls == 0, "an oversized turn must not cost a transcription"

    def test_an_oversized_transcript_is_refused_rather_than_truncated(self):
        # Cutting a transcript changes what the student said before anything grades it.
        rt = runtime(FakeStt("x" * (MAX_TRANSCRIPT_CHARACTERS + 1)))

        turn = run(rt)

        assert turn.failure is TurnFailure.TRANSCRIPT_TOO_LONG
        assert turn.transcript is None

    def test_the_transcript_cap_matches_the_other_interview_paths(self):
        from app.services.voice_interview import MAX_TRANSCRIPT_CHARACTERS as INTERVIEW_CAP

        assert MAX_TRANSCRIPT_CHARACTERS == INTERVIEW_CAP


# --------------------------------------------------------------------------- recognition


class TestRecognitionFailures:
    def test_an_unconfigured_recogniser_is_a_typed_failure_not_a_crash(self):
        """The approved default. `STT_PROVIDER` is `none`, so this is the ordinary case."""
        rt = TurnRuntime(
            stt_factory=lambda: (_ for _ in ()).throw(SpeechNotConfigured("stt")),
            tts_factory=lambda: FakeTts(),
        )

        turn = run(rt)

        assert turn.failure is TurnFailure.STT_UNAVAILABLE
        assert turn.transcript is None
        assert turn.answer is None

    def test_silence_is_reported_as_nothing_recognised(self):
        turn = run(runtime(FakeStt("   ")))

        assert turn.failure is TurnFailure.NOTHING_RECOGNISED
        assert turn.transcript is None

    @pytest.mark.parametrize(
        "raised, expected",
        [
            (SpeechTimeout("fake-stt", 130.0, 120.0), TurnFailure.STT_TIMEOUT),
            (UnsupportedLanguage("fake-stt", "ta", frozenset({"en"})), TurnFailure.STT_UNSUPPORTED),
            (SpeechUnavailable("fake-stt", "weights missing"), TurnFailure.STT_UNAVAILABLE),
            (SpeechFailed("fake-stt", "decoder exploded"), TurnFailure.STT_FAILED),
            (RuntimeError("something nobody modelled"), TurnFailure.STT_FAILED),
        ],
    )
    def test_each_recogniser_failure_maps_to_its_own_code(self, raised, expected):
        turn = run(runtime(FakeStt(raises=raised)))

        assert turn.failure is expected

    def test_a_provider_reason_never_reaches_the_user_message(self):
        """`SpeechFailed` carries a reason that can name a model path or a file."""
        turn = run(runtime(FakeStt(raises=SpeechFailed("fake-stt", "/models/secret/path.bin"))))

        assert turn.failure is not None
        assert "secret" not in user_message_for(turn.failure)
        assert "path.bin" not in user_message_for(turn.failure)

    def test_an_unsupported_language_does_not_become_a_wrong_transcript(self):
        # tiny.en is English-only. The honest answer is a refusal, not a guess.
        turn = run(runtime(FakeStt(raises=UnsupportedLanguage("fake-stt", "hi", frozenset({"en"})))))

        assert turn.failure is TurnFailure.STT_UNSUPPORTED
        assert turn.transcript is None


# --------------------------------------------------------------------------- answering


class TestAnswering:
    def test_a_responder_that_raises_is_a_typed_failure_with_the_transcript_kept(self):
        async def broken(text: str) -> Answer:
            raise RuntimeError("the orchestrator fell over")

        turn = run(runtime(), respond=broken)

        assert turn.failure is TurnFailure.RESPONSE_FAILED
        # The transcript survives: recognition really did happen, and discarding it would make
        # the turn look like a recognition failure.
        assert turn.transcript == "I bisected the change and added a test."
        assert turn.answer is None

    def test_an_empty_answer_is_a_failure_rather_than_silence(self):
        async def empty(text: str) -> Answer:
            return Answer(text="   ")

        turn = run(runtime(), respond=empty)

        assert turn.failure is TurnFailure.RESPONSE_FAILED

    def test_an_ungrounded_answer_is_still_a_successful_turn(self):
        """"I cannot tell you that" is a correct answer, not a failed turn.

        This is the shape the coach uses when `job_provider` is `none`: it says live market data
        is unavailable, and that reply must reach the student as a reply.
        """

        async def honest(text: str) -> Answer:
            return Answer(text="I do not have live market data, so I cannot say.", grounded=False)

        turn = run(runtime(), respond=honest)

        assert turn.failure is None
        assert turn.speakable
        assert turn.answer is not None and turn.answer.grounded is False

    def test_the_tools_a_responder_reports_are_carried_for_the_client(self):
        async def with_tools(text: str) -> Answer:
            return Answer(text="here is your passport", tools_used=("skills.get_passport",))

        turn = run(runtime(), respond=with_tools)

        assert turn.answer is not None
        assert turn.answer.tools_used == ("skills.get_passport",)


# --------------------------------------------------------------------------- synthesis


class TestSynthesisFailures:
    def test_an_unconfigured_synthesizer_still_returns_the_answer(self):
        """The approved default again. A reply the student can read beats a failed turn --
        especially in interview mode, where the answer may already be stored."""
        rt = TurnRuntime(
            stt_factory=lambda: FakeStt(),
            tts_factory=lambda: (_ for _ in ()).throw(SpeechNotConfigured("tts")),
        )

        turn = run(rt)

        assert turn.failure is TurnFailure.TTS_UNAVAILABLE
        assert turn.answer is not None and turn.answer.text.startswith("You said:")
        assert turn.transcript is not None
        assert turn.speakable is False

    def test_empty_synthesized_audio_is_a_failure_not_a_silent_success(self):
        """The specification singles this out: a driver returning nothing has not spoken."""
        turn = run(runtime(tts=FakeTts(audio=b"")))

        assert turn.failure is TurnFailure.TTS_FAILED
        assert turn.speakable is False
        assert turn.answer is not None

    @pytest.mark.parametrize(
        "raised, expected",
        [
            (SpeechTimeout("fake-tts", 70.0, 60.0), TurnFailure.TTS_TIMEOUT),
            (SpeechUnavailable("fake-tts", "no driver on this host"), TurnFailure.TTS_UNAVAILABLE),
            (SpeechFailed("fake-tts", "driver crashed"), TurnFailure.TTS_FAILED),
            (RuntimeError("unmodelled"), TurnFailure.TTS_FAILED),
        ],
    )
    def test_each_synthesizer_failure_maps_to_its_own_code(self, raised, expected):
        turn = run(runtime(tts=FakeTts(raises=raised)))

        assert turn.failure is expected
        assert turn.answer is not None, "the answer survives a synthesis failure"


# --------------------------------------------------------------------------- cancellation


class TestCancellation:
    def test_cancelling_before_the_turn_stops_before_recognition(self):
        stt = FakeStt()
        cancel = CancellationToken()
        cancel.cancel()

        turn = run(runtime(stt), cancellation=cancel)

        assert turn.failure is TurnFailure.CANCELLED
        assert stt.calls == 0

    def test_cancelling_during_recognition_does_not_run_the_responder(self):
        called = []

        async def should_not_run(text: str) -> Answer:
            called.append(text)
            return Answer(text="nope")

        cancel = CancellationToken()

        class CancellingStt(FakeStt):
            def transcribe(self, request):
                cancel.cancel()  # arrives while recognition is in flight
                return Transcript(text="a real transcript", is_final=True)

        turn = run(runtime(CancellingStt()), respond=should_not_run, cancellation=cancel)

        assert turn.failure is TurnFailure.CANCELLED
        assert called == []
        # The transcript is kept: recognition genuinely completed before the cancel landed.
        assert turn.transcript == "a real transcript"

    def test_a_cancel_after_the_answer_keeps_the_answer_and_skips_only_the_speaking(self):
        """Interview mode is why: the answer may already be stored authoritatively, so
        discarding it would tell the student nothing happened when something did."""
        cancel = CancellationToken()
        tts = FakeTts()

        async def answer_then_cancel(text: str) -> Answer:
            cancel.cancel()
            return Answer(text="stored and graded")

        turn = run(runtime(tts=tts), respond=answer_then_cancel, cancellation=cancel)

        assert turn.failure is TurnFailure.CANCELLED
        assert turn.answer is not None and turn.answer.text == "stored and graded"
        assert turn.speakable is False
        assert tts.calls == 0


# --------------------------------------------------------------------------- the contract


class TestADishonestTurnCannotBeBuilt:
    def test_a_turn_with_no_failure_must_have_an_answer(self):
        with pytest.raises(ValueError):
            SpokenTurn(turn_id="t", generation_id="g", transcript="said")

    def test_speech_cannot_accompany_a_turn_that_produced_no_answer(self):
        with pytest.raises(ValueError):
            SpokenTurn(
                turn_id="t",
                generation_id="g",
                transcript="said",
                speech=Speech(audio=b"RIFF", audio_format=PCM),
                failure=TurnFailure.RESPONSE_FAILED,
            )

    def test_empty_audio_cannot_be_presented_as_speech(self):
        with pytest.raises(ValueError):
            SpokenTurn(
                turn_id="t",
                generation_id="g",
                transcript="said",
                answer=Answer(text="hello"),
                speech=Speech(audio=b"", audio_format=PCM),
            )

    def test_a_turn_that_recognised_nothing_cannot_carry_a_transcript(self):
        for failure in (TurnFailure.STT_UNAVAILABLE, TurnFailure.NOTHING_RECOGNISED):
            with pytest.raises(ValueError):
                SpokenTurn(turn_id="t", generation_id="g", transcript="invented", failure=failure)

    def test_every_failure_code_has_a_user_message(self):
        for failure in TurnFailure:
            message = user_message_for(failure)
            assert message and message[0].isupper()

    def test_no_user_message_leaks_a_code_or_internal_term(self):
        for failure in TurnFailure:
            message = user_message_for(failure).lower()
            for leaked in ("exception", "traceback", "stt_", "tts_", "none", "provider"):
                assert leaked not in message, f"{failure.value} message mentions {leaked!r}"


class TestTheTurnRecordsWhichVoiceSpoke:
    """So a turn that sounded wrong can be looked up afterwards.

    The female-voice defect was invisible in these metrics: a turn reported its provider, its
    latency and `outcome=ok` while speaking in the host's male default.

    Asserted through the emitted metrics line rather than by injecting a clock, because
    `TurnRuntime.run` owns its clock -- so this exercises the real path all the way to the
    record an operator would actually read.
    """

    def test_a_successful_turn_records_the_voice_outcome(self, caplog):
        from app.core.voice_selection import VoiceOutcome

        with caplog.at_level(logging.INFO, logger="app.voice.turn_metrics"):
            run(runtime(tts=VoiceReportingTts(VoiceOutcome.SELECTED)))
        assert "voice=selected" in caplog.text

    def test_a_fallback_voice_is_recorded_too(self, caplog):
        from app.core.voice_selection import VoiceOutcome

        with caplog.at_level(logging.INFO, logger="app.voice.turn_metrics"):
            run(runtime(tts=VoiceReportingTts(VoiceOutcome.UNAVAILABLE)))
        # The turn succeeds AND the record says the voice was not the configured one. Both
        # facts matter; reporting only the first is what hid this defect.
        assert "voice=unavailable" in caplog.text

    def test_a_provider_that_reports_no_voice_still_completes(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.voice.turn_metrics"):
            spoken = run(runtime(tts=FakeTts()))
        assert spoken.failure is None
        assert "voice=" not in caplog.text
