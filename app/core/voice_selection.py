"""One place that decides which voice the host speech driver speaks with.

## Why this module exists

The female-voice defect this fixes was not a missing voice, and not a wrong setting. It was
*two* synthesisers.

`OllamaProvider.synthesize_speech` in `app/ai/ollama_provider.py` had voice selection and
called it. `Pyttsx3TextToSpeech` in `app/speech/pyttsx3_tts.py` had none at all -- it called
`pyttsx3.init()` and went straight to `save_to_file`, so the driver kept its system default.
The Voice Career Coach runs through `app/voice/turn_runtime.py`, which resolves its
synthesiser from `TTS_PROVIDER` (`pyttsx3`) via `app.speech.selection.get_text_to_speech`,
and therefore reached the second one.

So every individual signal was true and the feature was still wrong: the configuration said
``female``, the source contained ``Zira``, a unit test asserted the selection, and the coach
spoke in "Microsoft David Desktop" -- male, and the host default on this machine.

Fixing the second adapter in isolation would leave the same shape in place: two
implementations, one of which quietly misses the next fix. Both now call this module, and
`tests/unit/test_voice_selection.py` asserts that every synthesis path does.

## What it guarantees

* The driver is *asked* for a voice through its own contract -- enumerate
  ``getProperty("voices")``, match, then ``setProperty("voice", id)`` -- rather than being
  left on whatever the host prefers.
* The selection is **read back and confirmed**, because a driver accepting a set call is not
  the same as a driver honouring it.
* The outcome is always reported. A preference that could not be satisfied becomes a named
  state, never a silent fall back to the system default. That silence is precisely what hid
  this defect for as long as it stayed hidden.

## Why this lives in `app/core` rather than `app/speech`

`tests/unit/test_speech_selection.py::test_nothing_in_the_ai_layer_imports_the_speech_layer`
forbids `app/ai/*` from importing `app.speech`, so that the text features cannot come to
depend on a speech provider being installable. Putting the shared selector in `app.speech`
and importing it from `ollama_provider` broke that rule -- correctly, and the test caught it.

Both layers already depend on `app.core`, and this module is stdlib-only with a single lazy
read of `app.core.config`, so `app/core` is the one home that serves both without weakening
the layering rule or duplicating the algorithm.

Nothing here logs or returns the text being spoken. Voice ids and voice names are
device configuration, not personal data; transcripts are neither, and never enter this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: espeak-ng reports ``gender`` as None for most voices, so the gender match cannot work
#: there. Its voice ids carry the variant instead: ``+f1``..``+f5`` female, ``+m1``..``+m7``
#: male. Matching the convention is a documented espeak behaviour, not a guess about names.
_ESPEAK_MARKERS = {"female": "+f", "male": "+m"}

#: Preferences that mean "do not touch the driver's choice". Spelled out so that leaving the
#: host default is something a deployment can *ask* for, and is reported as a decision rather
#: than being indistinguishable from the bug this module fixes.
_NO_PREFERENCE = frozenset({"", "system", "default"})


class VoiceOutcome(str, Enum):
    """What actually happened when the driver was asked for a voice.

    Six states rather than a bool because "the voice is wrong" has several causes that need
    different fixes, and collapsing them is how an operator ends up looking at the request
    when the real problem is an uninstalled voice package.
    """

    #: Asked for a voice, the driver took it, and the read-back agreed.
    SELECTED = "selected"
    #: The set call was accepted and queued but has not been confirmed yet.
    #:
    #: pyttsx3 does not apply a property immediately: `DriverProxy.setProperty` calls
    #: `self._push(...)`, so the write sits in the event queue until `runAndWait()` drains it,
    #: while `getProperty` reads the driver's *current* state. Reading back straight after the
    #: set therefore returns the OLD voice and proves nothing. Measured here: a read-back
    #: immediately after setting Zira returns David; the same read-back after `runAndWait()`
    #: returns Zira. Confirmation belongs after synthesis -- see `confirm_selection`.
    SELECTED_PENDING = "selected_pending"
    #: The driver accepted the set call but the read-back could not be evaluated. Some pyttsx3
    #: drivers do not implement the getter symmetrically; the set may well have worked. Not
    #: reported as success, because it has not been proven.
    SELECTED_UNVERIFIED = "selected_unverified"
    #: Configuration asked for the host default. Working as instructed.
    NO_PREFERENCE = "no_preference"
    #: The driver exposes no enumerable voice list, so there is nothing to choose between.
    NOT_ENUMERABLE = "not_enumerable"
    #: Voices enumerated, none matched the preference. The usual cause is that the voice is
    #: not installed on the host -- an OS-level fix, not a code one.
    UNAVAILABLE = "unavailable"
    #: A voice matched and the driver refused to take it.
    REJECTED = "rejected"
    #: Read-back proved the driver kept something else. The worst case, and the one a bare
    #: ``setProperty`` call cannot detect.
    NOT_RETAINED = "not_retained"

    @property
    def is_satisfied(self) -> bool:
        """True when the driver is speaking with the voice configuration asked for."""
        return self in (VoiceOutcome.SELECTED, VoiceOutcome.NO_PREFERENCE)

    @property
    def is_fallback(self) -> bool:
        """True when a preference was expressed and the host default is being used instead."""
        return self in (
            VoiceOutcome.NOT_ENUMERABLE,
            VoiceOutcome.UNAVAILABLE,
            VoiceOutcome.REJECTED,
            VoiceOutcome.NOT_RETAINED,
        )


@dataclass(frozen=True)
class VoiceChoice:
    """The result of asking a driver for a voice, in a form safe to log.

    `detail` carries driver error text, which can name a voice or a COM failure but never
    contains synthesised text -- this module is not given any.
    """

    outcome: VoiceOutcome
    requested: str
    voice_id: Optional[str] = None
    voice_name: Optional[str] = None
    available: int = 0
    detail: Optional[str] = None

    def describe(self) -> str:
        """A one-line, privacy-safe summary for diagnostics and logs."""
        parts = [f"outcome={self.outcome.value}", f"requested={self.requested or 'system'}"]
        if self.voice_name:
            parts.append(f"voice={self.voice_name}")
        parts.append(f"available={self.available}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)


def _gender_of(voice: Any) -> str:
    return str(getattr(voice, "gender", "") or "").strip().lower()


def _id_of(voice: Any) -> str:
    return str(getattr(voice, "id", "") or "")


def _name_of(voice: Any) -> str:
    return str(getattr(voice, "name", "") or "") or _id_of(voice)


def enumerate_voices(engine: Any) -> list[Any]:
    """The driver's voice list, or empty when it does not expose one.

    An exception here is not a failure: espeak-ng through some pyttsx3 versions has no
    enumerable list, and its default voice still speaks.
    """
    try:
        voices = engine.getProperty("voices")
    except Exception:  # noqa: BLE001 - driver-specific; pyttsx3 raises several types
        return []
    return list(voices or [])


def available_voice_ids(engine: Any) -> frozenset[str]:
    """Voice ids this driver accepts. Feeds `TtsCapabilities.voices`.

    The pyttsx3 adapter previously declared this empty, which the contract defines as "one
    voice". On this host that was false -- there are two, and one of them is the voice the
    product asks for.
    """
    return frozenset(vid for vid in (_id_of(v) for v in enumerate_voices(engine)) if vid)


def _match(voices: list[Any], preference: str) -> Optional[Any]:
    """The first voice matching `preference`, by driver metadata then espeak convention."""
    chosen = next((v for v in voices if _gender_of(v) == preference), None)
    if chosen is not None:
        return chosen
    marker = _ESPEAK_MARKERS.get(preference)
    if marker is None:
        # An explicit voice id or name rather than a gender word.
        lowered = preference.lower()
        return next(
            (v for v in voices if lowered in _id_of(v).lower() or lowered in _name_of(v).lower()),
            None,
        )
    return next((v for v in voices if marker in _id_of(v).lower()), None)


def select_voice(
    engine: Any,
    *,
    preference: Optional[str] = None,
    voice: Optional[str] = None,
) -> VoiceChoice:
    """Ask `engine` to speak with the configured voice, and report what happened.

    `voice` is an explicit per-request voice id or name and wins over `preference`, which is
    the deployment-wide gender word from ``TTS_VOICE_GENDER``. Both are matched by the same
    code path so an operator debugging one is debugging both.

    Never raises for an unsatisfiable preference: callers decide whether a wrong voice is
    worse than no audio, and `Pyttsx3TextToSpeech` makes that decision from
    ``TTS_VOICE_REQUIRED``. What this function will not do is return silently as though the
    preference had been honoured.
    """
    if voice:
        requested = voice.strip()
    else:
        from app.core.config import settings

        requested = (preference if preference is not None else settings.tts_voice_gender).strip()

    if requested.lower() in _NO_PREFERENCE:
        return VoiceChoice(VoiceOutcome.NO_PREFERENCE, requested)

    voices = enumerate_voices(engine)
    if not voices:
        return VoiceChoice(
            VoiceOutcome.NOT_ENUMERABLE, requested, detail="driver exposes no voice list"
        )

    chosen = _match(voices, requested.lower())
    if chosen is None:
        return VoiceChoice(
            VoiceOutcome.UNAVAILABLE,
            requested,
            available=len(voices),
            detail="no installed voice matches the preference",
        )

    chosen_id, chosen_name = _id_of(chosen), _name_of(chosen)
    try:
        engine.setProperty("voice", chosen_id)
    except Exception as exc:  # noqa: BLE001 - driver-specific
        return VoiceChoice(
            VoiceOutcome.REJECTED,
            requested,
            voice_id=chosen_id,
            voice_name=chosen_name,
            available=len(voices),
            detail=f"driver refused the voice ({type(exc).__name__})",
        )

    # Pending, not selected. The write is queued; confirming it here would read the old voice
    # back and report a failure that has not happened. `confirm_selection` resolves this once
    # the caller has run the engine.
    return VoiceChoice(
        VoiceOutcome.SELECTED_PENDING,
        requested,
        voice_id=chosen_id,
        voice_name=chosen_name,
        available=len(voices),
    )


def confirm_selection(engine: Any, choice: VoiceChoice) -> VoiceChoice:
    """Resolve a `SELECTED_PENDING` choice by reading the voice back after synthesis.

    Call this *after* the engine has been run (`runAndWait`), never before: pyttsx3 queues
    property writes, so a read-back taken earlier reports the previous voice and would turn a
    working selection into a reported failure.

    Anything other than a pending choice is returned unchanged, so callers can pass whatever
    `select_voice` gave them without branching.
    """
    if choice.outcome is not VoiceOutcome.SELECTED_PENDING:
        return choice

    try:
        current = engine.getProperty("voice")
    except Exception as exc:  # noqa: BLE001 - driver-specific
        return VoiceChoice(
            VoiceOutcome.SELECTED_UNVERIFIED,
            choice.requested,
            voice_id=choice.voice_id,
            voice_name=choice.voice_name,
            available=choice.available,
            detail=f"read-back unavailable ({type(exc).__name__})",
        )
    if not current:
        return VoiceChoice(
            VoiceOutcome.SELECTED_UNVERIFIED,
            choice.requested,
            voice_id=choice.voice_id,
            voice_name=choice.voice_name,
            available=choice.available,
            detail="read-back empty",
        )

    current = str(current)
    wanted = {(choice.voice_id or "").lower(), (choice.voice_name or "").lower()} - {""}
    if current.lower() in wanted:
        return VoiceChoice(
            VoiceOutcome.SELECTED,
            choice.requested,
            voice_id=choice.voice_id,
            voice_name=choice.voice_name,
            available=choice.available,
        )
    return VoiceChoice(
        VoiceOutcome.NOT_RETAINED,
        choice.requested,
        voice_id=choice.voice_id,
        voice_name=choice.voice_name,
        available=choice.available,
        detail="the driver kept a different voice",
    )


def apply_rate(engine: Any, rate: int) -> Optional[int]:
    """Set the speaking rate, returning what the driver reports afterwards.

    Separate from `select_voice` because rate is a comfort setting rather than an identity
    one, and a driver that cannot change it should still get the right voice. ``rate <= 0``
    means keep the driver default.
    """
    if rate <= 0:
        return None
    try:
        engine.setProperty("rate", rate)
        return int(engine.getProperty("rate"))
    except Exception:  # noqa: BLE001 - driver-specific
        return None


def log_choice(choice: VoiceChoice, *, where: str) -> None:
    """Log a selection at a level that matches how wrong it is.

    A fallback is a warning: the user is hearing a voice the product did not ask for, and
    that fact needs to be visible without anyone enabling debug logging -- the previous
    version of this logic reported it at no level at all.
    """
    if choice.outcome.is_fallback:
        logger.warning("%s is using a fallback voice: %s", where, choice.describe())
    else:
        logger.info("%s voice selection: %s", where, choice.describe())
