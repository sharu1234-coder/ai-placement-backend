"""The canonical STT/TTS contracts.

Framework-free on purpose: no FastAPI, no SQLAlchemy, no vendor SDK. An adapter imports its
own vendor library; nothing else in the app does, and nothing here knows a provider exists.

The shape of this module is a direct response to a bug the VA-0.5 work found in the text
provider: `AnthropicProvider` inherited `transcribe()` without implementing it, so selecting
it succeeded and the failure arrived later, after the upload had been read. The lesson
encoded here is that *"the interface has a method" and "this provider can do it" are different
questions*, and the second must be answerable before any work starts. So:

* Capabilities are **derived** from what a subclass actually overrides, never hand-declared,
  for everything that can be derived (streaming, partial transcripts). A hand-written flag is
  a second source of truth, and the disagreement between the two is the bug.
* Everything that genuinely cannot be derived (which sample rates, which languages, which
  container formats) is declared as data -- and `tests/unit/test_speech_capability_parity.py`
  asserts the declaration matches observable behaviour for every registered provider.
* Unsupported work is refused **before** execution, with a typed error.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional

# --------------------------------------------------------------------------- audio formats


class AudioEncoding(str, Enum):
    """How a block of audio bytes is laid out.

    Only what the system actually handles today. `PCM16` is the realtime wire format (VA-2
    sends raw little-endian PCM16 binary frames); `WAV` is the interview TTS response format;
    `M4A` is the interview upload format (`audio/mp4` AAC in an MP4 container). An encoding is
    added here when a provider exists that reads or writes it, not in anticipation.
    """

    PCM16 = "pcm16"
    WAV = "wav"
    M4A = "m4a"


@dataclass(frozen=True)
class AudioFormat:
    """A fully-specified audio format. No field has a default.

    Defaults are how a 48 kHz stereo stream gets fed to a 16 kHz mono recogniser and comes out
    as confident nonsense: every caller silently inherits an assumption nobody checked. A
    format must therefore be stated, and conversion must be asked for explicitly
    (`app.speech.audio`).
    """

    encoding: AudioEncoding
    sample_rate_hz: int
    channel_count: int

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError(f"sample rate must be positive, got {self.sample_rate_hz}")
        if self.channel_count <= 0:
            raise ValueError(f"channel count must be positive, got {self.channel_count}")

    @property
    def bytes_per_frame(self) -> int:
        """Bytes for one sample across all channels. Container formats have no fixed answer."""
        if self.encoding is not AudioEncoding.PCM16:
            raise ValueError(f"{self.encoding.value} is a container format with no fixed frame size")
        return 2 * self.channel_count

    def duration_seconds(self, byte_count: int) -> float:
        """Duration of `byte_count` bytes of PCM in this format."""
        return byte_count / (self.bytes_per_frame * self.sample_rate_hz)

    def describe(self) -> str:
        return f"{self.encoding.value} {self.sample_rate_hz} Hz {self.channel_count}ch"


#: The realtime path's format (VoiceAudioConfig.DEFAULT_SAMPLE_RATE_HZ on Android, and
#: DEFAULT_SAMPLE_RATE_HZ in app/voice/realtime_protocol.py). Named so an adapter can state
#: "I accept this" instead of repeating three numbers that would then drift.
REALTIME_PCM16: AudioFormat = AudioFormat(AudioEncoding.PCM16, 16_000, 1)


# --------------------------------------------------------------------------- failures


class SpeechError(Exception):
    """Base for every speech failure.

    Separate from `AIGenerationError` rather than inheriting it. The two mean different things
    to a caller -- "the model wrote invalid JSON" and "the microphone format is unsupported"
    need different responses -- and an existing `except AIGenerationError` must not start
    swallowing speech failures by accident. The HTTP mapping, where one is needed, is the
    route's decision, not this module's.
    """


class SpeechUnsupported(SpeechError):
    """The provider cannot do this *at all*. Raised before any work begins.

    Retrying is pointless and falling back is only valid if another provider's declared
    capabilities cover the request, so this is never a transient failure.
    """


class UnsupportedAudioFormat(SpeechUnsupported):
    def __init__(self, provider: str, requested: AudioFormat, reason: str = "") -> None:
        self.provider = provider
        self.requested = requested
        detail = f" ({reason})" if reason else ""
        super().__init__(f"{provider} cannot accept {requested.describe()}{detail}")


class UnsupportedLanguage(SpeechUnsupported):
    def __init__(self, provider: str, language: str, supported: frozenset[str]) -> None:
        self.provider = provider
        self.language = language
        self.supported = supported
        super().__init__(
            f"{provider} does not support language '{language}' (supports: {sorted(supported) or 'none declared'})"
        )


class UnsupportedOperation(SpeechUnsupported):
    """Streaming asked of a provider that only does batch, and similar.

    The specific reason this type exists: a batch-only provider that answered a streaming call
    by returning one final chunk would look like working streaming while delivering none of
    its only benefit -- the first partial arriving early. Silence about that is worse than a
    refusal, because the latency regression shows up on a device, late.
    """

    def __init__(self, provider: str, operation: str, reason: str = "") -> None:
        self.provider = provider
        self.operation = operation
        detail = f" ({reason})" if reason else ""
        super().__init__(f"{provider} does not support {operation}{detail}")


class SpeechUnavailable(SpeechError):
    """The provider exists but could not be reached or initialised. Possibly transient."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        super().__init__(f"{provider} is unavailable: {reason}")


class SpeechFailed(SpeechError):
    """The provider was reachable and the request was valid, but the work failed.

    Distinct from `SpeechUnavailable` because only this one means "the request got as far as
    the model": a caller may reasonably retry an unavailable provider and should not blindly
    retry this, since the same audio will usually fail the same way.
    """

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        super().__init__(f"{provider} failed: {reason}")


class SpeechTimeout(SpeechError):
    def __init__(self, provider: str, elapsed_seconds: float, budget_seconds: float) -> None:
        self.provider = provider
        self.elapsed_seconds = elapsed_seconds
        self.budget_seconds = budget_seconds
        # .3g rather than .1f: a sub-second budget renders as "0.0s" under .1f, which reads as
        # a bug in the message rather than the small budget it is.
        super().__init__(f"{provider} exceeded its {budget_seconds:.3g}s budget after {elapsed_seconds:.3g}s")


class SpeechCancelled(SpeechError):
    """The caller cancelled. Not a failure of the provider, and never retried."""

    def __init__(self, provider: str, stage: str) -> None:
        self.provider = provider
        self.stage = stage
        super().__init__(f"{provider} was cancelled while {stage}")


class MalformedProviderOutput(SpeechError):
    """The provider returned something outside its own contract.

    Its own class because the correct response differs from a timeout: this is a provider bug
    or a version skew, and retrying reproduces it. An empty or header-only WAV is the case
    already seen in practice -- see `app/ai/ollama_provider.py`, where a TTS driver that
    "succeeded" while writing nothing would otherwise have reached a client as audio.
    """

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        super().__init__(f"{provider} returned unusable output: {reason}")


# --------------------------------------------------------------------------- cancellation


class CancellationToken:
    """Cooperative cancellation.

    Cooperative rather than pre-emptive because the underlying work is a blocking C call
    (CTranslate2 inference, a TTS driver write) that cannot be interrupted from Python. So
    cancellation is honest about its granularity: an adapter checks the token at the points
    where control genuinely returns to it -- between transcript segments, between synthesis
    chunks -- and `raise_if_cancelled` names the stage so a caller can see where it stopped.
    An adapter that claimed instant cancellation would be lying about the C stack.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self, provider: str, stage: str) -> None:
        if self._cancelled:
            raise SpeechCancelled(provider, stage)


class Deadline:
    """A wall-clock budget, checked at the same points as cancellation.

    Same honesty constraint: this bounds the work at segment granularity, not instantly. A
    single enormous segment can overrun, which is why adapters also bound their *input*
    duration -- the two together are what make the cost of one request predictable.
    """

    __slots__ = ("budget_seconds", "_started")

    def __init__(self, budget_seconds: float) -> None:
        if budget_seconds <= 0:
            raise ValueError("a deadline needs a positive budget")
        self.budget_seconds = budget_seconds
        self._started = time.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def expired(self) -> bool:
        return self.elapsed_seconds > self.budget_seconds

    def raise_if_expired(self, provider: str) -> None:
        if self.expired:
            raise SpeechTimeout(provider, self.elapsed_seconds, self.budget_seconds)


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class Transcript:
    """One transcription result.

    `is_final` exists so a streaming provider's partials and a batch provider's single answer
    have the same type: a consumer that renders partials must not have to know which kind of
    provider produced them. A batch provider returns exactly one, with `is_final=True`.
    """

    text: str
    is_final: bool
    language: Optional[str] = None
    #: 0.0--1.0 where the provider supplies one, else None. Never fabricated: a provider with
    #: no confidence signal leaves this None rather than returning a plausible number, because
    #: a caller thresholding on an invented value is worse off than one that knows it has none.
    confidence: Optional[float] = None
    #: Duration of the audio this covers, where known.
    audio_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in 0.0--1.0, got {self.confidence}")


@dataclass(frozen=True)
class Speech:
    """One synthesis result. `audio` is bytes in `audio_format`, nothing else."""

    audio: bytes
    audio_format: AudioFormat
    audio_seconds: Optional[float] = None
    #: Wall-clock time until the first audio byte was available, where the adapter can measure
    #: it honestly. A batch provider measures the whole call, which is its real first-audio
    #: latency; a streaming one measures the first chunk. None means not measured, not zero.
    first_audio_latency_seconds: Optional[float] = None


# --------------------------------------------------------------------------- capabilities


@dataclass(frozen=True)
class SttCapabilities:
    """What an STT provider accepts. Declared data, checked against behaviour by tests."""

    input_formats: frozenset[AudioEncoding]
    #: Rates the provider accepts for **raw PCM**. An adapter may convert internally, but only
    #: explicitly and boundedly (`app.speech.audio`), and only to a rate listed here -- a rate
    #: that is absent is refused rather than quietly approximated. Container encodings carry
    #: their own rate, so this is not checked for them; see `check_supported`.
    sample_rates_hz: frozenset[int]
    max_channel_count: int
    #: BCP-47-ish primary subtags ("en", "hi"). Empty means the provider auto-detects.
    languages: frozenset[str]
    #: Longest single request. Bounds cost and memory; a request over it is refused, not
    #: truncated, because silently dropping the end of an answer is a wrong transcript.
    max_audio_seconds: float
    supplies_confidence: bool
    #: Must agree with whether the class actually overrides `stream_transcribe`. Not a second
    #: source of truth: `validate_declaration` refuses a provider where the two disagree, and
    #: the registry calls it on construction, so an inconsistent provider cannot be selected.
    streaming: bool = False
    partial_transcripts: bool = False


@dataclass(frozen=True)
class TtsCapabilities:
    """What a TTS provider produces."""

    output_format: AudioFormat
    languages: frozenset[str]
    max_characters: int
    #: Voice identifiers this provider accepts, if it exposes a choice. Empty means one voice.
    voices: frozenset[str] = field(default_factory=frozenset)
    streaming: bool = False


# --------------------------------------------------------------------------- the interfaces


@dataclass(frozen=True)
class TranscribeRequest:
    """Everything an STT call needs, stated.

    A request object rather than six positional arguments because the audio format travels
    *with* the audio. Passing bytes and hoping the callee assumes the same rate is the failure
    mode this whole module exists to prevent.
    """

    audio: bytes
    audio_format: AudioFormat
    language: Optional[str] = None
    cancellation: Optional[CancellationToken] = None
    #: Overrides the provider's default budget when set.
    budget_seconds: Optional[float] = None


@dataclass(frozen=True)
class SynthesizeRequest:
    text: str
    language: str = "en"
    voice: Optional[str] = None
    cancellation: Optional[CancellationToken] = None
    budget_seconds: Optional[float] = None


class SpeechToText(ABC):
    """Provider-neutral STT."""

    name: str = "unknown"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" not in cls.__dict__ and cls.name == SpeechToText.name:
            cls.name = cls.__name__

    @property
    @abstractmethod
    def capabilities(self) -> SttCapabilities:
        """What this provider accepts. Must be cheap: called before every request."""

    @abstractmethod
    def transcribe(self, request: TranscribeRequest) -> Transcript:
        """Batch transcription. Implementations call `check_supported` first."""

    def stream_transcribe(self, request: TranscribeRequest) -> Iterator[Transcript]:
        """Incremental transcription, yielding partials then exactly one final.

        Default: refuse. A provider implements this only when it genuinely streams, and
        `declares_streaming` reads whether it did -- so "supports streaming" is a fact about
        the code rather than a claim in a config file.
        """
        raise UnsupportedOperation(self.name, "streaming transcription")

    @classmethod
    def declares_streaming(cls) -> bool:
        return cls.stream_transcribe is not SpeechToText.stream_transcribe

    def validate_declaration(self) -> None:
        """Refuse a provider whose declared capabilities contradict its code.

        Called by the registry when a provider is constructed, so the contradiction is caught
        at selection time rather than at the call site -- which is the whole point. A provider
        claiming streaming it has not implemented is the exact shape of the VA-0.5 bug.
        """
        caps = self.capabilities
        implemented = type(self).declares_streaming()
        if caps.streaming != implemented:
            raise SpeechUnsupported(
                f"{self.name} declares streaming={caps.streaming} but "
                f"{'overrides' if implemented else 'does not override'} stream_transcribe"
            )
        if caps.partial_transcripts and not implemented:
            raise SpeechUnsupported(f"{self.name} claims partial transcripts without implementing streaming")
        if not caps.input_formats:
            raise SpeechUnsupported(f"{self.name} declares no input format, so no request can ever be valid")
        if not caps.sample_rates_hz:
            raise SpeechUnsupported(f"{self.name} declares no sample rate, so no request can ever be valid")

    def check_supported(self, request: TranscribeRequest) -> None:
        """Refuse an impossible request before doing any work.

        Called at the top of `transcribe`. The order matters: cheapest and most specific
        first, so the error a caller sees names the actual problem rather than whichever check
        happened to run.
        """
        caps = self.capabilities
        fmt = request.audio_format
        if fmt.encoding not in caps.input_formats:
            raise UnsupportedAudioFormat(
                self.name, fmt, f"accepts {sorted(e.value for e in caps.input_formats)}"
            )
        # Only raw PCM has a rate worth checking here: a container states its own rate in its
        # header, and the value a caller passes alongside it is at best a guess. Checking that
        # guess would reject valid files and accept invalid ones.
        if fmt.encoding is AudioEncoding.PCM16 and fmt.sample_rate_hz not in caps.sample_rates_hz:
            raise UnsupportedAudioFormat(self.name, fmt, f"accepts {sorted(caps.sample_rates_hz)} Hz")
        if fmt.channel_count > caps.max_channel_count:
            raise UnsupportedAudioFormat(self.name, fmt, f"accepts at most {caps.max_channel_count} channel(s)")
        if request.language and caps.languages and request.language not in caps.languages:
            raise UnsupportedLanguage(self.name, request.language, caps.languages)
        if not request.audio:
            raise MalformedProviderOutput(self.name, "no audio was supplied")
        if fmt.encoding is AudioEncoding.PCM16:
            if len(request.audio) % fmt.bytes_per_frame:
                raise UnsupportedAudioFormat(
                    self.name,
                    fmt,
                    f"{len(request.audio)} bytes is not a whole number of samples, so the stream is misaligned",
                )
            seconds = fmt.duration_seconds(len(request.audio))
            if seconds > caps.max_audio_seconds:
                raise UnsupportedAudioFormat(
                    self.name, fmt, f"{seconds:.1f}s exceeds the {caps.max_audio_seconds:.0f}s limit"
                )


class TextToSpeech(ABC):
    """Provider-neutral TTS."""

    name: str = "unknown"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" not in cls.__dict__ and cls.name == TextToSpeech.name:
            cls.name = cls.__name__

    @property
    @abstractmethod
    def capabilities(self) -> TtsCapabilities:
        ...

    @abstractmethod
    def synthesize(self, request: SynthesizeRequest) -> Speech:
        ...

    def stream_synthesize(self, request: SynthesizeRequest) -> Iterator[Speech]:
        """Chunked synthesis. Default: refuse -- see `SpeechToText.stream_transcribe`."""
        raise UnsupportedOperation(self.name, "streaming synthesis")

    @classmethod
    def declares_streaming(cls) -> bool:
        return cls.stream_synthesize is not TextToSpeech.stream_synthesize

    def validate_declaration(self) -> None:
        """See `SpeechToText.validate_declaration`."""
        caps = self.capabilities
        implemented = type(self).declares_streaming()
        if caps.streaming != implemented:
            raise SpeechUnsupported(
                f"{self.name} declares streaming={caps.streaming} but "
                f"{'overrides' if implemented else 'does not override'} stream_synthesize"
            )
        if caps.max_characters <= 0:
            raise SpeechUnsupported(f"{self.name} declares a non-positive character limit")

    def check_supported(self, request: SynthesizeRequest) -> None:
        caps = self.capabilities
        if not request.text.strip():
            raise MalformedProviderOutput(self.name, "no text was supplied")
        if len(request.text) > caps.max_characters:
            raise UnsupportedOperation(
                self.name,
                "this request",
                f"{len(request.text)} characters exceeds the {caps.max_characters} limit",
            )
        if caps.languages and request.language not in caps.languages:
            raise UnsupportedLanguage(self.name, request.language, caps.languages)
        if request.voice and caps.voices and request.voice not in caps.voices:
            raise UnsupportedOperation(self.name, f"voice '{request.voice}'", f"has {sorted(caps.voices)}")
