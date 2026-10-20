"""Voice selection: that it happens, on every path, and that it is checked at the right time.

Three distinct defects are pinned here, because all three were live at once and each would
have survived a fix to the others.

1. **The Voice Career Coach never selected a voice.** Selection existed only in
   `app/ai/ollama_provider.py`, on the AI-provider path. The coach runs
   `app/voice/turn_runtime.py` -> `app/speech/pyttsx3_tts.py`, which called `pyttsx3.init()`
   and went straight to `save_to_file`. So the configuration said ``female``, the source
   contained ``Zira``, a test asserted the selection, and the coach still spoke in the host
   default -- "Microsoft David Desktop", male. `test_every_tts_path_asks_the_driver_for_a_voice`
   is the guard.

2. **Verifying the selection too early reports a working driver as broken.** pyttsx3 queues
   property writes -- `DriverProxy.setProperty` calls `self._push(...)` -- so a read-back
   taken straight after the set returns the *previous* voice. Measured on this host: setting
   Zira then reading back immediately returns David; reading back after `runAndWait()` returns
   Zira. `FakeVoiceEngine` below reproduces that queueing, so this suite fails if confirmation
   moves back before synthesis.

3. **A driver can accept a voice and keep another.** `setProperty` returning without raising
   is not evidence. Only the post-synthesis read-back is.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pytest

from app.core.voice_selection import (
    VoiceOutcome,
    available_voice_ids,
    confirm_selection,
    log_choice,
    select_voice,
)
from app.speech.audio import write_wav
from app.speech.contracts import SpeechUnavailable, SynthesizeRequest
from app.speech.pyttsx3_tts import Pyttsx3TextToSpeech

DAVID = r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_DAVID_11.0"
ZIRA = r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_ZIRA_11.0"


class FakeVoice:
    def __init__(self, id: str, name: str, gender: str | None) -> None:
        self.id = id
        self.name = name
        self.gender = gender
        self.age = "Adult"
        self.languages = ["en-US"]


def sapi_voices() -> list[FakeVoice]:
    """The two voices measured on the development host, in the order the driver lists them.

    David first, deliberately: he is the default, so a test that passes with him first is
    testing selection rather than luck.
    """
    return [
        FakeVoice(DAVID, "Microsoft David Desktop - English (United States)", "Male"),
        FakeVoice(ZIRA, "Microsoft Zira Desktop - English (United States)", "Female"),
    ]


def espeak_voices() -> list[FakeVoice]:
    """espeak-ng as pyttsx3 surfaces it: no gender metadata, variant encoded in the id."""
    return [
        FakeVoice("gmw/en", "english", None),
        FakeVoice("gmw/en+f3", "english+f3", None),
    ]


class FakeVoiceEngine:
    """A pyttsx3 engine, including the part that matters: property writes are **queued**.

    `setProperty` records a pending value and `getProperty` keeps returning the old one until
    `runAndWait` drains it. That is not an embellishment -- it is the measured behaviour of
    pyttsx3's `DriverProxy`, and it is the reason a read-back placed before synthesis reports
    a false failure.
    """

    def __init__(
        self,
        voices: list[FakeVoice] | None = None,
        *,
        payload: bytes | None = None,
        enumerable: bool = True,
        set_error: Exception | None = None,
        honours_writes: bool = True,
    ) -> None:
        self._voices = voices if voices is not None else sapi_voices()
        self._current = self._voices[0].id if self._voices else ""
        self._pending: str | None = None
        self._rate = 200
        self.payload = payload
        self.enumerable = enumerable
        self.set_error = set_error
        #: False models a driver that takes the set call and keeps its own voice anyway.
        self.honours_writes = honours_writes
        self.written: list[str] = []
        self.set_calls: list[tuple[str, object]] = []

    def getProperty(self, name):  # noqa: N802 - pyttsx3's own name
        if name == "voices":
            if not self.enumerable:
                raise RuntimeError("this driver exposes no voice list")
            return list(self._voices)
        if name == "voice":
            return self._current
        if name == "rate":
            return self._rate
        raise KeyError(name)

    def setProperty(self, name, value):  # noqa: N802 - pyttsx3's own name
        self.set_calls.append((name, value))
        if name == "voice":
            if self.set_error is not None:
                raise self.set_error
            self._pending = str(value)
        elif name == "rate":
            self._rate = int(value)

    def save_to_file(self, text, path):
        self.written.append(text)
        if self.payload is not None:
            with open(path, "wb") as handle:
                handle.write(self.payload)

    def runAndWait(self):  # noqa: N802 - pyttsx3's own name
        if self._pending is not None:
            if self.honours_writes:
                self._current = self._pending
            self._pending = None


def good_wav(seconds: float = 0.5, rate: int = 22_050) -> bytes:
    samples = (
        np.sin(np.arange(int(seconds * rate), dtype=np.float32) / rate * 2 * math.pi * 300) * 0.4
    )
    return write_wav(samples, sample_rate_hz=rate)


def adapter(engine: FakeVoiceEngine) -> Pyttsx3TextToSpeech:
    return Pyttsx3TextToSpeech(engine_factory=lambda: engine)


@pytest.fixture(autouse=True)
def _default_voice_settings(monkeypatch):
    """Pin the settings every test reads, so none of them depends on the host's .env."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "tts_voice_gender", "female", raising=False)
    monkeypatch.setattr(settings, "tts_voice_required", False, raising=False)
    monkeypatch.setattr(settings, "tts_voice_rate", 0, raising=False)


class TestSelection:
    def test_the_configured_female_voice_is_chosen_over_the_default(self):
        engine = FakeVoiceEngine()
        choice = select_voice(engine)
        assert choice.voice_id == ZIRA
        assert "Zira" in (choice.voice_name or "")
        # The whole defect in one assertion: the driver must be *asked*.
        assert ("voice", ZIRA) in engine.set_calls

    def test_a_queued_write_is_pending_not_failed(self):
        """The bug this test exists for was mine, and it was in the fix.

        Confirming immediately after `setProperty` reads the driver's current voice -- still
        David, because the write is queued -- and reporting that as `NOT_RETAINED` turns a
        working selection into a loud false alarm.
        """
        engine = FakeVoiceEngine()
        choice = select_voice(engine)
        assert choice.outcome is VoiceOutcome.SELECTED_PENDING
        assert engine.getProperty("voice") == DAVID, "the fake must queue, like pyttsx3 does"
        assert not choice.outcome.is_fallback

    def test_confirmation_after_the_queue_drains_proves_the_selection(self):
        engine = FakeVoiceEngine()
        choice = select_voice(engine)
        engine.runAndWait()
        confirmed = confirm_selection(engine, choice)
        assert confirmed.outcome is VoiceOutcome.SELECTED
        assert confirmed.outcome.is_satisfied

    def test_a_driver_that_keeps_a_different_voice_is_caught(self):
        engine = FakeVoiceEngine(honours_writes=False)
        choice = select_voice(engine)
        engine.runAndWait()
        confirmed = confirm_selection(engine, choice)
        assert confirmed.outcome is VoiceOutcome.NOT_RETAINED
        assert confirmed.outcome.is_fallback
        assert not confirmed.outcome.is_satisfied

    def test_a_missing_female_voice_is_reported_not_hidden(self):
        engine = FakeVoiceEngine(voices=[sapi_voices()[0]])
        choice = select_voice(engine)
        assert choice.outcome is VoiceOutcome.UNAVAILABLE
        assert choice.outcome.is_fallback
        assert ("voice", ZIRA) not in engine.set_calls

    def test_a_driver_with_no_voice_list_is_a_named_state(self):
        choice = select_voice(FakeVoiceEngine(enumerable=False))
        assert choice.outcome is VoiceOutcome.NOT_ENUMERABLE

    def test_a_driver_that_refuses_the_voice_is_a_named_state(self):
        choice = select_voice(FakeVoiceEngine(set_error=RuntimeError("COM error")))
        assert choice.outcome is VoiceOutcome.REJECTED
        assert choice.voice_id == ZIRA

    def test_espeak_variants_are_matched_without_gender_metadata(self):
        """espeak reports gender None, so the gender match cannot work there.

        Without the id-convention fallback a Linux host would silently keep its male default
        -- the same defect, on the platform that is actually production-bound.
        """
        engine = FakeVoiceEngine(voices=espeak_voices())
        choice = select_voice(engine)
        assert choice.voice_id == "gmw/en+f3"

    def test_system_preference_leaves_the_driver_alone(self):
        engine = FakeVoiceEngine()
        choice = select_voice(engine, preference="system")
        assert choice.outcome is VoiceOutcome.NO_PREFERENCE
        assert choice.outcome.is_satisfied
        assert engine.set_calls == []

    def test_an_explicit_voice_overrides_the_gender_preference(self):
        engine = FakeVoiceEngine()
        choice = select_voice(engine, voice=DAVID)
        assert choice.voice_id == DAVID

    def test_voice_ids_are_reported_for_the_capability(self):
        assert available_voice_ids(FakeVoiceEngine()) == frozenset({DAVID, ZIRA})
        assert available_voice_ids(FakeVoiceEngine(enumerable=False)) == frozenset()


class TestAdapterUsesTheSelection:
    """The adapter the Voice Career Coach actually resolves, end to end."""

    def test_synthesis_speaks_with_the_configured_voice(self):
        engine = FakeVoiceEngine(payload=good_wav())
        provider = adapter(engine)
        provider.synthesize(SynthesizeRequest(text="Hey, how are you?"))
        # Not "setProperty was called" -- the voice the driver ended up on.
        assert engine.getProperty("voice") == ZIRA
        assert provider.last_voice_choice.outcome is VoiceOutcome.SELECTED

    def test_the_adapter_reports_a_fallback_rather_than_claiming_success(self):
        engine = FakeVoiceEngine(voices=[sapi_voices()[0]], payload=good_wav())
        provider = adapter(engine)
        speech = provider.synthesize(SynthesizeRequest(text="hello"))
        # Audio still returned: a wrong voice beats silence, by documented choice.
        assert speech.audio
        assert provider.last_voice_choice.outcome is VoiceOutcome.UNAVAILABLE
        assert not provider.last_voice_choice.outcome.is_satisfied

    def test_a_required_voice_that_is_missing_fails_the_turn(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "tts_voice_required", True, raising=False)
        provider = adapter(FakeVoiceEngine(voices=[sapi_voices()[0]], payload=good_wav()))
        with pytest.raises(SpeechUnavailable) as caught:
            provider.synthesize(SynthesizeRequest(text="hello"))
        assert "could not be selected" in str(caught.value)

    def test_a_required_voice_the_driver_silently_drops_fails_the_turn(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "tts_voice_required", True, raising=False)
        provider = adapter(FakeVoiceEngine(honours_writes=False, payload=good_wav()))
        with pytest.raises(SpeechUnavailable):
            provider.synthesize(SynthesizeRequest(text="hello"))

    def test_the_declared_voices_reflect_the_real_driver(self):
        engine = FakeVoiceEngine(payload=good_wav())
        provider = adapter(engine)
        provider.synthesize(SynthesizeRequest(text="hello"))
        # Previously hardcoded empty, which the contract reads as "one voice" -- false here.
        assert provider.capabilities.voices == frozenset({DAVID, ZIRA})

    def test_the_configured_rate_reaches_the_driver(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "tts_voice_rate", 175, raising=False)
        engine = FakeVoiceEngine(payload=good_wav())
        adapter(engine).synthesize(SynthesizeRequest(text="hello"))
        assert engine.getProperty("rate") == 175

    def test_rate_zero_leaves_the_driver_default_alone(self):
        engine = FakeVoiceEngine(payload=good_wav())
        adapter(engine).synthesize(SynthesizeRequest(text="hello"))
        assert engine.getProperty("rate") == 200
        assert not any(name == "rate" for name, _ in engine.set_calls)


class TestNoLeakage:
    def test_a_choice_never_carries_the_spoken_text(self):
        engine = FakeVoiceEngine(payload=good_wav())
        provider = adapter(engine)
        secret = "My salary expectation is forty lakhs and my PAN is ABCDE1234F"
        provider.synthesize(SynthesizeRequest(text=secret))
        described = provider.last_voice_choice.describe()
        assert "salary" not in described
        assert "ABCDE1234F" not in described
        assert "Zira" in described

    def test_the_selection_log_names_the_voice_and_not_the_text(self, caplog):
        engine = FakeVoiceEngine()
        choice = select_voice(engine)
        with caplog.at_level(logging.INFO):
            log_choice(choice, where="test")
        assert "Zira" in caplog.text
        assert "selected_pending" in caplog.text

    def test_a_fallback_is_logged_as_a_warning(self, caplog):
        """A fallback the operator cannot see is how this defect survived.

        It was reported at no level at all before, so nothing in the logs distinguished the
        wrong voice from the right one.
        """
        choice = select_voice(FakeVoiceEngine(voices=[sapi_voices()[0]]))
        with caplog.at_level(logging.WARNING):
            log_choice(choice, where="test")
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "fallback" in caplog.text


class TestEveryPathSelects:
    def test_every_tts_path_asks_the_driver_for_a_voice(self):
        """The guard against the original defect returning.

        Two synthesis paths exist, and the gap between them was the bug. This asserts both go
        through the one selector, by behaviour rather than by grepping for the import.
        """
        from app.ai.ollama_provider import _select_voice

        engine = FakeVoiceEngine()
        name = _select_voice(engine)
        assert "Zira" in (name or ""), "the AI-provider path must select a voice"
        assert ("voice", ZIRA) in engine.set_calls

        coach_engine = FakeVoiceEngine(payload=good_wav())
        adapter(coach_engine).synthesize(SynthesizeRequest(text="hello"))
        assert ("voice", ZIRA) in coach_engine.set_calls, "the coach path must select a voice"


class TestTurnMetrics:
    def test_a_turn_records_how_voice_selection_resolved(self):
        from app.voice.turn_metrics import TurnMetrics

        metrics = TurnMetrics(
            turn_id="t1",
            generation_id=1,
            outcome="ok",
            total_ms=10,
            tts_voice_outcome=VoiceOutcome.SELECTED.value,
        )
        assert "voice=selected" in metrics.render()

    def test_the_outcome_field_is_a_closed_vocabulary(self):
        from app.voice.turn_metrics import TurnMetrics

        with pytest.raises(ValueError, match="selection outcome"):
            TurnMetrics(
                turn_id="t1",
                generation_id=1,
                outcome="ok",
                total_ms=10,
                tts_voice_outcome="sounded nice",
            )
