"""One spoken turn: recognise it, answer it, speak the answer.

This is the piece VA-12 found missing. The realtime vocabulary was agreed in VA-2 and the Android
client was built to receive all of it, but the gateway's only send sites were `session_accepted`
and `session_expired` -- so every conversational message type was declared and never emitted.
This module produces the result those messages describe.

**What it owns:** the order of the three stages, the bounds on each, cancellation, and the
invariant that a turn cannot describe itself dishonestly.

**What it deliberately does not own:** the answer. The `respond` step is injected, so this module
never imports the orchestrator or the interview engine and cannot grow a second opinion about
grading, progression or grounding. The gateway supplies a responder built from the socket's
already-authenticated identity; nothing here can manufacture one.

**Blocking work.** Recognition and synthesis are CPU-bound local inference and a host speech
driver respectively. Both are handed to a worker thread, because this runs on the event loop that
is also serving every other socket -- a 6-second transcription inline would stall them all. The
thread boundary is also the only honest place to admit what cancellation can and cannot do: a
`CancellationToken` stops the *next* stage, it does not interrupt a driver mid-utterance.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

from app.speech.audio import pcm16_duration_seconds
from app.speech.contracts import (
    AudioFormat,
    CancellationToken,
    Speech,
    SpeechCancelled,
    SpeechError,
    SpeechTimeout,
    SpeechUnavailable,
    SpeechUnsupported,
    SynthesizeRequest,
    TranscribeRequest,
    Transcript,
)
from app.voice.speech_text import for_speech
from app.voice.turn_metrics import OK as _METRICS_OK
from app.voice.turn_metrics import TurnClock

logger = logging.getLogger(__name__)

#: The same ceiling the typed and spoken interview answers already share. Taken from
#: `voice_interview.MAX_TRANSCRIPT_CHARACTERS` rather than chosen again: a realtime turn that
#: accepted more would mean the three paths disagree about what a valid answer is.
MAX_TRANSCRIPT_CHARACTERS = 5_000

#: One turn of 16 kHz mono PCM16 is 32 000 bytes a second, so this is about three minutes --
#: longer than any interview answer and long enough for a rambling coaching question. It bounds
#: what one turn can cost before recognition is even attempted.
MAX_TURN_AUDIO_BYTES = 6 * 1024 * 1024

#: Recognition and synthesis budgets. Generous because the approved adapters are local CPU
#: inference on a developer machine, not a hosted service with a latency guarantee.
DEFAULT_TRANSCRIBE_BUDGET_SECONDS = 120.0
DEFAULT_SYNTHESIZE_BUDGET_SECONDS = 60.0


class VoiceMode(str, Enum):
    """Which authoritative path answers the turn."""

    COACH = "coach"
    INTERVIEW = "interview"


class TurnFailure(str, Enum):
    """Typed codes, safe to put on the wire and in a log line.

    Never a provider's own words: `SpeechFailed` carries a reason that can name a model path, a
    driver or a file, and `SpeechUnavailable` can name a missing dependency. Those are logged,
    not sent.
    """

    AUDIO_TOO_LARGE = "audio_too_large"
    NO_AUDIO = "no_audio"
    STT_UNAVAILABLE = "stt_unavailable"
    STT_UNSUPPORTED = "stt_unsupported"
    STT_TIMEOUT = "stt_timeout"
    STT_FAILED = "stt_failed"
    NOTHING_RECOGNISED = "nothing_recognised"
    TRANSCRIPT_TOO_LONG = "transcript_too_long"
    RESPONSE_FAILED = "response_failed"
    TTS_UNAVAILABLE = "tts_unavailable"
    TTS_TIMEOUT = "tts_timeout"
    TTS_FAILED = "tts_failed"
    CANCELLED = "cancelled"


#: What a student can be told for each code. Deliberately a closed mapping: a message built by
#: interpolating an exception is how a provider's internals reach a screen.
_USER_MESSAGE = {
    TurnFailure.AUDIO_TOO_LARGE: "That was too long to process. Try a shorter answer.",
    TurnFailure.NO_AUDIO: "I did not receive any audio for that turn.",
    TurnFailure.STT_UNAVAILABLE: "Speech recognition is not available on this server.",
    TurnFailure.STT_UNSUPPORTED: "This audio format is not supported by the recogniser.",
    TurnFailure.STT_TIMEOUT: "Recognising that took too long. Please try again.",
    TurnFailure.STT_FAILED: "I could not make out that recording. Please try again.",
    TurnFailure.NOTHING_RECOGNISED: "I did not hear anything. Please try again.",
    TurnFailure.TRANSCRIPT_TOO_LONG: "That answer was longer than this interview accepts.",
    TurnFailure.RESPONSE_FAILED: "I could not prepare a reply just now.",
    TurnFailure.TTS_UNAVAILABLE: "I have a reply but cannot speak it on this server.",
    TurnFailure.TTS_TIMEOUT: "Speaking that reply took too long.",
    TurnFailure.TTS_FAILED: "I have a reply but could not speak it.",
    TurnFailure.CANCELLED: "That turn was cancelled.",
}


def user_message_for(failure: TurnFailure) -> str:
    return _USER_MESSAGE[failure]


@dataclass(frozen=True)
class Answer:
    """What a responder produced. `text` is what will be spoken and shown."""

    text: str
    #: Catalogue capability names that actually ran, for `tool_call_started`. Never free text.
    tools_used: tuple[str, ...] = ()
    #: True when the responder reached authoritative state; false when it is reporting a
    #: limitation (no live market data, evaluation not ready). The turn is still a success --
    #: an honest "I cannot tell you that" is a correct answer, not a failure.
    grounded: bool = True


@dataclass(frozen=True)
class SpokenTurn:
    """The authoritative result of one spoken turn.

    `__post_init__` is what makes a dishonest turn unconstructible, in the same spirit as
    `ActionOutcome` in the tool registry: the combinations below are not merely discouraged,
    they cannot be built.
    """

    turn_id: str
    generation_id: str
    transcript: Optional[str] = None
    answer: Optional[Answer] = None
    speech: Optional[Speech] = None
    failure: Optional[TurnFailure] = None

    def __post_init__(self) -> None:
        if self.failure is None and self.answer is None:
            raise ValueError("a turn without a failure must have an answer")
        if self.speech is not None and self.answer is None:
            raise ValueError("speech cannot be sent for a turn that produced no answer")
        if self.speech is not None and not self.speech.audio:
            # An empty buffer presented as synthesized speech is the "silently return empty
            # audio as if generation succeeded" case the specification singles out.
            raise ValueError("speech must carry audio; report a TTS failure instead")
        if self.failure in (TurnFailure.STT_UNAVAILABLE, TurnFailure.NOTHING_RECOGNISED) and self.transcript:
            raise ValueError("a turn that recognised nothing cannot carry a transcript")

    @property
    def recognised(self) -> bool:
        return bool(self.transcript)

    @property
    def speakable(self) -> bool:
        return self.speech is not None and bool(self.speech.audio)


#: Supplied by the gateway, built from the socket's authenticated identity. Async so a responder
#: may do its own thread hand-off for database work.
Responder = Callable[[str], Awaitable[Answer]]

#: Called once, as soon as a transcript exists and has passed its bounds -- before the responder
#: runs. The gateway uses it to put the student's own words on screen while the agent is still
#: thinking, instead of holding them back until the whole turn finishes. Not a return value,
#: because by the time `run` returns the transcript is minutes old on a slow CPU.
TranscriptObserver = Callable[[str], Awaitable[None]]


class TurnRuntime:
    """Runs the three stages in order, with bounds and honest failures.

    The speech providers are resolved through callables so a test can supply a fake without
    touching configuration, and so a missing provider is a *typed failure at the moment it is
    needed* rather than an import-time crash. Both default to the real selection functions,
    which raise `SpeechNotConfigured` while `STT_PROVIDER`/`TTS_PROVIDER` are `none` -- the
    approved default.
    """

    def __init__(
        self,
        *,
        stt_factory: Optional[Callable[[], object]] = None,
        tts_factory: Optional[Callable[[], object]] = None,
        transcribe_budget_seconds: float = DEFAULT_TRANSCRIBE_BUDGET_SECONDS,
        synthesize_budget_seconds: float = DEFAULT_SYNTHESIZE_BUDGET_SECONDS,
        max_turn_audio_bytes: int = MAX_TURN_AUDIO_BYTES,
    ) -> None:
        self._stt_factory = stt_factory or _default_stt
        self._tts_factory = tts_factory or _default_tts
        self._transcribe_budget = transcribe_budget_seconds
        self._synthesize_budget = synthesize_budget_seconds
        self._max_turn_audio_bytes = max_turn_audio_bytes

    async def run(
        self,
        *,
        pcm: bytes,
        audio_format: AudioFormat,
        respond: Responder,
        turn_id: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
        language: str = "en",
        on_transcript: Optional[TranscriptObserver] = None,
    ) -> SpokenTurn:
        turn = turn_id or new_turn_id()
        generation = new_generation_id()
        cancel = cancellation or CancellationToken()
        # Started before the first bounds check, so `total_ms` covers the whole turn including a
        # refusal -- a turn rejected for oversized audio still took time the caller waited for.
        clock = TurnClock(turn_id=turn, generation_id=generation)
        try:
            clock.audio_ms = int(pcm16_duration_seconds(len(pcm), audio_format) * 1000)
        except Exception:  # noqa: BLE001 - a non-PCM16 format has no frame-derived duration
            clock.audio_ms = None

        def failed(
            failure: TurnFailure,
            *,
            transcript: Optional[str] = None,
            cancelled_at: Optional[str] = None,
        ) -> SpokenTurn:
            clock.finish(
                outcome=failure.value,
                # A length, never the words. See `turn_metrics` on why content cannot reach here.
                transcript_chars=len(transcript) if transcript is not None else None,
                cancelled_at=cancelled_at,
            ).emit()
            return SpokenTurn(
                turn_id=turn, generation_id=generation, transcript=transcript, failure=failure
            )

        if not pcm:
            return failed(TurnFailure.NO_AUDIO)
        if len(pcm) > self._max_turn_audio_bytes:
            # Refused before recognition, so an oversized turn costs a length check rather than
            # minutes of CPU.
            logger.info("voice turn %s refused: %d audio bytes", turn, len(pcm))
            return failed(TurnFailure.AUDIO_TOO_LARGE)
        if cancel.cancelled:
            return failed(TurnFailure.CANCELLED, cancelled_at="before_recognition")

        # ---------------------------------------------------------------- recognise
        recognised = clock.mark()
        try:
            transcript = await self._transcribe(pcm, audio_format, language, cancel, clock)
        except _TypedSpeechFailure as exc:
            clock.stt_ms = recognised()
            logger.info("voice turn %s recognition failed: %s", turn, exc.failure.value)
            return failed(exc.failure)

        clock.stt_ms = recognised()
        text = (transcript.text or "").strip()
        if not text:
            return failed(TurnFailure.NOTHING_RECOGNISED)
        if len(text) > MAX_TRANSCRIPT_CHARACTERS:
            # Refused rather than truncated: cutting a transcript changes what the student said
            # before anything grades or answers it.
            return failed(TurnFailure.TRANSCRIPT_TOO_LONG, transcript=None)
        if cancel.cancelled:
            return failed(
                TurnFailure.CANCELLED, transcript=text, cancelled_at="after_recognition"
            )

        if on_transcript is not None:
            # After the cancellation check, so a turn the student has already abandoned does not
            # put its transcript on their screen. Before `respond`, because recognition is the
            # slow stage and the words exist now.
            await on_transcript(text)

        # ---------------------------------------------------------------- answer
        answered = clock.mark()
        try:
            answer = await respond(text)
        except Exception as exc:  # noqa: BLE001 - the responder's own typed errors are its business
            clock.respond_ms = answered()
            logger.warning("voice turn %s responder failed: %s", turn, type(exc).__name__)
            return failed(TurnFailure.RESPONSE_FAILED, transcript=text)
        clock.respond_ms = answered()

        if not answer.text.strip():
            return failed(TurnFailure.RESPONSE_FAILED, transcript=text)
        if cancel.cancelled:
            # The answer exists and is returned, because it is authoritative and may already
            # have changed interview state. Only the speaking of it is skipped.
            clock.finish(
                outcome=TurnFailure.CANCELLED.value,
                transcript_chars=len(text),
                tools_used=answer.tools_used,
                cancelled_at="after_response",
            ).emit()
            return SpokenTurn(
                turn_id=turn,
                generation_id=generation,
                transcript=text,
                answer=answer,
                failure=TurnFailure.CANCELLED,
            )

        # ---------------------------------------------------------------- speak
        spoken = clock.mark()
        try:
            # The synthesizer gets the answer rendered for listening; the client still gets
            # `answer.text` verbatim. Notation only -- no sentence, number, warning or
            # qualification is dropped, so the spoken and displayed answers mean the same
            # thing. Done here rather than in a second wire field, which would need protocol
            # parity on both sides for what is a presentation concern.
            speech = await self._synthesize(for_speech(answer.text), language, cancel, clock)
        except _TypedSpeechFailure as exc:
            clock.tts_ms = spoken()
            # The answer still goes back. A reply the student can read is worth more than a
            # failed turn, and the interview state it may reflect has already changed.
            logger.info("voice turn %s synthesis failed: %s", turn, exc.failure.value)
            clock.finish(
                outcome=exc.failure.value,
                transcript_chars=len(text),
                tools_used=answer.tools_used,
            ).emit()
            return SpokenTurn(
                turn_id=turn,
                generation_id=generation,
                transcript=text,
                answer=answer,
                failure=exc.failure,
            )

        clock.tts_ms = spoken()
        clock.finish(
            outcome=_METRICS_OK,
            transcript_chars=len(text),
            speech_bytes=len(speech.audio),
            tools_used=answer.tools_used,
            # The adapter's figure where it has one, rather than this layer guessing at it.
            tts_first_audio_ms=(
                int(speech.first_audio_latency_seconds * 1000)
                if speech.first_audio_latency_seconds is not None
                else None
            ),
        ).emit()
        return SpokenTurn(
            turn_id=turn,
            generation_id=generation,
            transcript=text,
            answer=answer,
            speech=speech,
        )

    # ------------------------------------------------------------------ stages

    async def _transcribe(
        self,
        pcm: bytes,
        audio_format: AudioFormat,
        language: str,
        cancel: CancellationToken,
        clock: Optional[TurnClock] = None,
    ) -> Transcript:
        try:
            provider = self._stt_factory()
        except Exception as exc:  # noqa: BLE001 - SpeechNotConfigured and any import failure
            logger.info("no speech recogniser is configured: %s", type(exc).__name__)
            raise _TypedSpeechFailure(TurnFailure.STT_UNAVAILABLE) from exc
        if clock is not None:
            # The class name, which is what separates a real-inference measurement from one taken
            # against a test fake. Never a configuration value or a credential.
            clock.stt_provider = type(provider).__name__

        request = TranscribeRequest(
            audio=pcm,
            audio_format=audio_format,
            language=language,
            cancellation=cancel,
            budget_seconds=self._transcribe_budget,
        )
        try:
            # A worker thread, not the event loop: this is local CPU inference.
            return await asyncio.to_thread(provider.transcribe, request)
        except SpeechCancelled as exc:
            raise _TypedSpeechFailure(TurnFailure.CANCELLED) from exc
        except SpeechTimeout as exc:
            raise _TypedSpeechFailure(TurnFailure.STT_TIMEOUT) from exc
        except SpeechUnsupported as exc:
            raise _TypedSpeechFailure(TurnFailure.STT_UNSUPPORTED) from exc
        except SpeechUnavailable as exc:
            raise _TypedSpeechFailure(TurnFailure.STT_UNAVAILABLE) from exc
        except (SpeechError, Exception) as exc:  # noqa: BLE001
            raise _TypedSpeechFailure(TurnFailure.STT_FAILED) from exc

    async def _synthesize(
        self,
        text: str,
        language: str,
        cancel: CancellationToken,
        clock: Optional[TurnClock] = None,
    ) -> Speech:
        try:
            provider = self._tts_factory()
        except Exception as exc:  # noqa: BLE001
            logger.info("no speech synthesizer is configured: %s", type(exc).__name__)
            raise _TypedSpeechFailure(TurnFailure.TTS_UNAVAILABLE) from exc
        if clock is not None:
            clock.tts_provider = type(provider).__name__

        request = SynthesizeRequest(
            text=text,
            language=language,
            cancellation=cancel,
            budget_seconds=self._synthesize_budget,
        )
        try:
            speech = await asyncio.to_thread(provider.synthesize, request)
        except SpeechCancelled as exc:
            raise _TypedSpeechFailure(TurnFailure.CANCELLED) from exc
        except SpeechTimeout as exc:
            raise _TypedSpeechFailure(TurnFailure.TTS_TIMEOUT) from exc
        except (SpeechUnsupported, SpeechUnavailable) as exc:
            raise _TypedSpeechFailure(TurnFailure.TTS_UNAVAILABLE) from exc
        except (SpeechError, Exception) as exc:  # noqa: BLE001
            raise _TypedSpeechFailure(TurnFailure.TTS_FAILED) from exc
        finally:
            # In a finally so a turn that fails during or after synthesis still records which
            # voice spoke -- a wrong-voice turn that also timed out is exactly the case worth
            # being able to look up. Read off the adapter rather than recomputed: only the
            # adapter knows what the driver actually accepted.
            if clock is not None:
                choice = getattr(provider, "last_voice_choice", None)
                if choice is not None:
                    clock.tts_voice_outcome = choice.outcome.value

        if not speech.audio:
            # A driver that returns nothing has not spoken. Reported as a failure rather than
            # sent as silence the client would play as if it were a reply.
            raise _TypedSpeechFailure(TurnFailure.TTS_FAILED)
        return speech


class _TypedSpeechFailure(Exception):
    """Internal: carries a safe code out of a stage without the provider's own words."""

    def __init__(self, failure: TurnFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


def new_turn_id() -> str:
    return f"t-{uuid.uuid4().hex[:12]}"


def new_generation_id() -> str:
    return f"g-{uuid.uuid4().hex[:12]}"


def _default_stt() -> object:
    from app.speech.selection import get_speech_to_text

    return get_speech_to_text()


def _default_tts() -> object:
    from app.speech.selection import get_text_to_speech

    return get_text_to_speech()
