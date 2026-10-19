"""Stage timings for one spoken turn, in a shape that cannot carry what the student said.

## Why this exists

`LS §13.3 (p.18)` requires six performance metrics for the Voice Agent and **none of them had
ever been measured**. Every latency figure quoted anywhere in this project so far has been about
something else: an adapter benchmark, a real-time factor, a test duration.

## Why it is a closed record rather than a log call

The obvious implementation is `logger.info("turn %s took %s", turn, elapsed)` at a few points.
The reason this module exists instead is that voice metrics sit directly beside the two things
that must never be logged: the transcript of what a student said, and the answer given back. A
free-form log line one edit away from `%s` on the transcript is a privacy incident waiting for a
careless debugging session.

So [TurnMetrics] has **no field that can hold free text**, and `__post_init__` enforces it:
`outcome` and `cancelled_at` must come from closed vocabularies, `tools_used` entries must match
the catalogue's `family.verb` shape, and provider names must look like class names. Content
appears only as *lengths* -- `transcript_chars`, `speech_bytes` -- which answer "was there an
answer, how big" without revealing a word of it. Making the leak unconstructible is the same
tactic `SpokenTurn.__post_init__` and `ActionOutcome` already use: a rule enforced by the type is
not a rule anyone has to remember.

## What is deliberately not measured

**Per-tool invocation and completion times.** The active coach path runs the whole read fan-out
inside one synchronous `Orchestrator.coach()` call on a worker thread, so the runtime sees one
`respond` boundary and nothing finer. `Answer.tools_used` tells us *which* capabilities ran, and
that is recorded; *when each started* is not available, and is not invented. It is the same
limitation that makes `tool_call_started` a forbidden protocol frame.

**Partial-transcript or streaming latency.** The only approved recogniser is turn-based. There is
no first-partial moment to measure, so no such metric is reported.

**True interruption latency.** `cancelled_at` names the checkpoint at which the runtime *noticed*
cancellation, and `total_ms` says when that was. That is an honest observation latency, not the
time until work actually stopped: cancelling a turn cannot interrupt a recogniser already running
inside a worker thread -- its result is simply discarded. Reporting this as "interruption latency"
would overstate it, which is why the field is named for the checkpoint instead.

**Anything about a device.** Every number here is measured on the server, between the committed
turn and the synthesized reply. It excludes capture, upload, playback and the network, so it is
not a spoken-turn latency as a student experiences it. The first of `LS §13.3`'s metrics -- time
from user stop to first spoken response -- still needs a handset.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.voice_selection import VoiceOutcome

logger = logging.getLogger(__name__)

#: The stage boundaries at which the runtime can observe cancellation. A closed set, so this
#: field cannot become a free-text note about why a turn stopped.
CANCELLATION_CHECKPOINTS = frozenset(
    {"before_recognition", "after_recognition", "after_response"}
)

#: Catalogue names are `family.verb`, e.g. `jobs.save`. Anything else is not a capability name.
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

#: Adapter identification is a class name, which is what distinguishes a run against real local
#: inference from one against a test fake -- `FasterWhisperSpeechToText` vs `FakeStt`.
_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Values `tts_voice_outcome` accepts. Sourced from the enum rather than restated, so the
#: vocabulary cannot drift from the code that produces it.
_VOICE_OUTCOMES = frozenset(outcome.value for outcome in VoiceOutcome)

_OK = "ok"


def _now() -> float:
    """The one place this module reads a clock.

    An indirection rather than calling `time.monotonic` at each site, because the two sites
    were not equivalent: a `default_factory=time.monotonic` binds the function object when
    the class is defined, so it kept returning the real clock while the elapsed-time helper
    read a substituted one. The result was a start in the future and every duration clamped
    to zero. Reading through one function makes the clock substitutable as a unit, which is
    what a test needs, and removes the chance of the two drifting apart again.
    """
    return time.monotonic()


@dataclass(frozen=True)
class TurnMetrics:
    """One turn's timings. Durations are milliseconds from a monotonic clock.

    `None` means "this stage did not run", which is different from zero and is why the stage
    durations are optional: a turn refused for empty audio has no `stt_ms`, and recording 0
    there would put a fictional measurement into any average taken over these lines.
    """

    turn_id: str
    generation_id: str
    #: `TurnFailure.value`, or `"ok"`. Closed by construction -- the runtime passes an enum value.
    outcome: str
    total_ms: int
    audio_ms: Optional[int] = None
    stt_ms: Optional[int] = None
    respond_ms: Optional[int] = None
    tts_ms: Optional[int] = None
    #: The adapter's own first-audio latency, when it measures one. Not re-derived here:
    #: `Speech.first_audio_latency_seconds` is the only honest source, and a batch provider
    #: legitimately reports the whole call. `None` means not measured, never zero.
    tts_first_audio_ms: Optional[int] = None
    transcript_chars: Optional[int] = None
    speech_bytes: Optional[int] = None
    tools_used: tuple[str, ...] = ()
    cancelled_at: Optional[str] = None
    stt_provider: Optional[str] = None
    tts_provider: Optional[str] = None
    #: How voice selection actually resolved for this turn, as a `VoiceOutcome` value.
    #:
    #: Recorded because the female-voice defect was invisible in exactly these metrics: the
    #: turn reported a provider, a latency and an outcome of OK while speaking in the wrong
    #: voice. A closed vocabulary rather than the voice name, for the same reason
    #: `cancelled_at` is one -- it must not become a free-text field, and the voice name is
    #: already in the adapter's own log line.
    tts_voice_outcome: Optional[str] = None

    def __post_init__(self) -> None:
        if self.cancelled_at is not None and self.cancelled_at not in CANCELLATION_CHECKPOINTS:
            raise ValueError(
                f"cancelled_at must name a known checkpoint, not {self.cancelled_at!r}; "
                "this field is a closed vocabulary so it cannot carry a free-text note"
            )
        for name in self.tools_used:
            if not _CAPABILITY.match(name):
                raise ValueError(
                    f"tools_used must contain catalogue names like 'jobs.save', not {name!r}"
                )
        for label, value in (("stt_provider", self.stt_provider), ("tts_provider", self.tts_provider)):
            if value is not None and not _CLASS_NAME.match(value):
                raise ValueError(f"{label} must be an adapter class name, not {value!r}")
        if self.tts_voice_outcome is not None and self.tts_voice_outcome not in _VOICE_OUTCOMES:
            raise ValueError(
                f"tts_voice_outcome must name a known selection outcome, not "
                f"{self.tts_voice_outcome!r}; known: {sorted(_VOICE_OUTCOMES)}"
            )
        for label, value in (
            ("total_ms", self.total_ms),
            ("audio_ms", self.audio_ms),
            ("stt_ms", self.stt_ms),
            ("respond_ms", self.respond_ms),
            ("tts_ms", self.tts_ms),
            ("tts_first_audio_ms", self.tts_first_audio_ms),
            ("transcript_chars", self.transcript_chars),
            ("speech_bytes", self.speech_bytes),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{label} cannot be negative ({value})")

    @property
    def real_time_factor(self) -> Optional[float]:
        """Recognition time over audio duration. Below 1.0 means faster than real time.

        The standard way to say whether a recogniser can keep up on this host. `None` when either
        side is unknown, rather than a divide-by-zero or a made-up 0.
        """
        if not self.stt_ms or not self.audio_ms:
            return None
        return round(self.stt_ms / self.audio_ms, 3)

    def render(self) -> str:
        """A single stable line. Only ids, numbers, closed vocabularies and class names."""
        parts = [
            f"turn={self.turn_id}",
            f"gen={self.generation_id}",
            f"outcome={self.outcome}",
            f"total_ms={self.total_ms}",
        ]
        for label, value in (
            ("audio_ms", self.audio_ms),
            ("stt_ms", self.stt_ms),
            ("respond_ms", self.respond_ms),
            ("tts_ms", self.tts_ms),
            ("tts_first_audio_ms", self.tts_first_audio_ms),
            ("transcript_chars", self.transcript_chars),
            ("speech_bytes", self.speech_bytes),
        ):
            if value is not None:
                parts.append(f"{label}={value}")
        if self.real_time_factor is not None:
            parts.append(f"rtf={self.real_time_factor}")
        if self.stt_provider:
            parts.append(f"stt={self.stt_provider}")
        if self.tts_provider:
            parts.append(f"tts={self.tts_provider}")
        if self.tts_voice_outcome:
            parts.append(f"voice={self.tts_voice_outcome}")
        if self.tools_used:
            parts.append(f"tools={','.join(self.tools_used)}")
        if self.cancelled_at:
            parts.append(f"cancelled_at={self.cancelled_at}")
        return "voice turn metrics " + " ".join(parts)

    def emit(self) -> None:
        """INFO, unconditionally.

        Not gated on the environment, because nothing here is sensitive or development-specific:
        it is durations, counts, typed outcome codes and adapter class names. What *is*
        development-only is the speech adapters themselves, and `validate_for_production()`
        already refuses those outside development. Gating the measurement as well would mean the
        one environment that can produce a real figure is the one that never records it.
        """
        logger.info("%s", self.render())


@dataclass
class TurnClock:
    """Monotonic stage timing for one turn. Mutable by design; the record it builds is frozen.

    `time.monotonic` rather than `time.time`: a wall clock can step backwards over an NTP
    correction or a DST change and hand a negative duration to whatever averages these.
    """

    turn_id: str
    generation_id: str
    #: `lambda: _now()`, not `default_factory=_now`: the latter binds the function object
    #: when the class is defined, which put the start on a different clock from the
    #: elapsed-time helper and clamped every duration to zero. The lambda resolves `_now`
    #: from module globals at call time, so there is genuinely one clock.
    _started: float = field(default_factory=lambda: _now())
    audio_ms: Optional[int] = None
    stt_ms: Optional[int] = None
    respond_ms: Optional[int] = None
    tts_ms: Optional[int] = None
    stt_provider: Optional[str] = None
    tts_provider: Optional[str] = None
    tts_voice_outcome: Optional[str] = None

    @staticmethod
    def _ms(start: float) -> int:
        return max(0, int((_now() - start) * 1000))

    def mark(self):
        """A stage start. Returns a callable that records the elapsed milliseconds."""
        start = _now()
        return lambda: self._ms(start)

    @property
    def total_ms(self) -> int:
        return self._ms(self._started)

    def finish(
        self,
        *,
        outcome: str,
        transcript_chars: Optional[int] = None,
        speech_bytes: Optional[int] = None,
        tools_used: tuple[str, ...] = (),
        cancelled_at: Optional[str] = None,
        tts_first_audio_ms: Optional[int] = None,
    ) -> TurnMetrics:
        return TurnMetrics(
            turn_id=self.turn_id,
            generation_id=self.generation_id,
            outcome=outcome,
            total_ms=self.total_ms,
            audio_ms=self.audio_ms,
            stt_ms=self.stt_ms,
            respond_ms=self.respond_ms,
            tts_ms=self.tts_ms,
            tts_first_audio_ms=tts_first_audio_ms,
            transcript_chars=transcript_chars,
            speech_bytes=speech_bytes,
            tools_used=tools_used,
            cancelled_at=cancelled_at,
            stt_provider=self.stt_provider,
            tts_provider=self.tts_provider,
            tts_voice_outcome=self.tts_voice_outcome,
        )


OK = _OK
