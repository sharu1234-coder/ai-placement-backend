"""The development speech configuration artifact, checked against the guard that refuses it.

`tests/unit/test_config_validation.py` already proves the production guard rejects
`faster-whisper` and `pyttsx3`, using those names as literals. This file closes a different gap:
there is now a *file* in the repository that tells a developer which providers to switch on, and
nothing stopped that file from drifting into naming something the guard would happily accept in
production. The literals in the guard's tests would still pass.

So these tests parse `.env.development.speech` and feed what they find to the real validator. The
file is data here, not documentation: if someone adds a provider to it, the provider has to be one
production refuses, or this fails.

They are config tests. No adapter is loaded, no inference runs, and nothing here is evidence that
speech works -- that is `tests/unit/test_speech_real_model.py` (real adapters) and
`tests/api/test_voice_realtime_real_adapters.py` (real adapters through the gateway).
"""

from pathlib import Path

import pytest

from app.core.config import (
    _DEVELOPMENT_ONLY_STT,
    _DEVELOPMENT_ONLY_TTS,
    Settings,
    settings,
    validate_for_production,
)
from app.speech import selection
from app.speech.selection import SpeechNotConfigured
from tests.unit.test_config_validation import _production_base, _production_ready_ai

_ARTIFACT = Path(__file__).resolve().parents[2] / ".env.development.speech"


@pytest.fixture(scope="module")
def declared() -> dict:
    """The artifact's settings, parsed. Fails loudly if the file is gone or empty."""
    assert _ARTIFACT.is_file(), (
        f"{_ARTIFACT.name} is missing. It is the only version-controlled record of what "
        "development speech means; the guard tests below would otherwise check nothing."
    )
    found = {}
    for line in _ARTIFACT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        found[key.strip()] = value.strip()
    assert found, f"{_ARTIFACT.name} declares no settings at all"
    return found


class TestTheArtifactIsDevelopmentOnly:
    def test_it_identifies_itself_as_development(self, declared) -> None:
        """Self-identifying rather than relying on the default.

        `environment` defaults to "development", so omitting it would work -- and would also
        mean a developer could paste these lines into a production env file and see nothing
        wrong with them. Stating it makes the intent part of the artifact.
        """
        assert declared.get("ENVIRONMENT") == "development"

    def test_every_provider_it_names_is_one_production_refuses(self, declared) -> None:
        """The load-bearing assertion.

        A new provider added to this file must already be in the development-only sets. If it is
        not, the file is recommending something the production guard would let through, which is
        how an unapproved provider reaches a deployment.
        """
        stt = declared.get("STT_PROVIDER")
        tts = declared.get("TTS_PROVIDER")
        assert stt in _DEVELOPMENT_ONLY_STT, (
            f"{_ARTIFACT.name} selects STT_PROVIDER={stt!r}, which the production guard does "
            f"not refuse (development-only: {sorted(_DEVELOPMENT_ONLY_STT)})"
        )
        assert tts in _DEVELOPMENT_ONLY_TTS, (
            f"{_ARTIFACT.name} selects TTS_PROVIDER={tts!r}, which the production guard does "
            f"not refuse (development-only: {sorted(_DEVELOPMENT_ONLY_TTS)})"
        )

    def test_the_providers_it_names_are_implemented(self, declared) -> None:
        # A typo here would be found at the first committed turn, as a NotImplementedError from
        # the selector rather than as a configuration error.
        assert declared["STT_PROVIDER"] in selection.STT_PROVIDERS
        assert declared["TTS_PROVIDER"] in selection.TTS_PROVIDERS

    def test_applying_it_to_production_is_a_startup_failure(self, declared, monkeypatch) -> None:
        """The artifact's own values through the real validator, not retyped literals."""
        # The neighbouring suite's own helpers, so "an otherwise production-ready config" has
        # one definition. Without them the validator stops at the first unrelated blocker and
        # this test would pass on the wrong refusal.
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        monkeypatch.setattr(settings, "stt_provider", declared["STT_PROVIDER"])
        monkeypatch.setattr(settings, "tts_provider", declared["TTS_PROVIDER"])

        with pytest.raises(RuntimeError, match="STT_PROVIDER/TTS_PROVIDER"):
            validate_for_production()

    def test_it_carries_no_secret(self, declared) -> None:
        """It is committed, unlike `.env`, so it must never grow a credential.

        An allow-list rather than a scan for suspicious names: a provider that needed a key
        would have to be added here deliberately, and the review that adds it is the point at
        which someone asks whether a committed file is the right home for it.
        """
        assert set(declared) <= {"ENVIRONMENT", "STT_PROVIDER", "TTS_PROVIDER"}, (
            f"{_ARTIFACT.name} declares settings beyond the speech selection: "
            f"{sorted(set(declared) - {'ENVIRONMENT', 'STT_PROVIDER', 'TTS_PROVIDER'})}"
        )


class TestTheDefaultsAreStillClosed:
    """The artifact exists; the defaults must not have moved because of it."""

    def test_a_fresh_settings_object_selects_no_speech_provider(self) -> None:
        # Constructed with the file loader bypassed, so a developer's own `.env` -- which may
        # legitimately have these lines appended -- cannot make this test pass or fail.
        fresh = Settings(_env_file=None)
        assert fresh.stt_provider == "none"
        assert fresh.tts_provider == "none"

    def test_the_closed_default_raises_the_typed_unavailable_condition(self, monkeypatch) -> None:
        """What a stock server does: refuse, with the typed error the runtime turns into
        `stt_unavailable`/`tts_unavailable`. Never a fabricated transcript, never silence
        presented as speech."""
        monkeypatch.setattr(settings, "stt_provider", "none")
        monkeypatch.setattr(settings, "tts_provider", "none")

        with pytest.raises(SpeechNotConfigured):
            selection.get_speech_to_text()
        with pytest.raises(SpeechNotConfigured):
            selection.get_text_to_speech()

    def test_an_unknown_provider_is_refused_rather_than_silently_substituted(self) -> None:
        with pytest.raises(NotImplementedError):
            selection.get_speech_to_text("elevenlabs")
        with pytest.raises(NotImplementedError):
            selection.get_text_to_speech("elevenlabs")

    def test_no_paid_or_external_provider_is_selectable_at_all(self) -> None:
        """ElevenLabs is excluded by the owner, and the registry is closed, not filtered."""
        assert selection.STT_PROVIDERS == frozenset({"none", "faster-whisper"})
        assert selection.TTS_PROVIDERS == frozenset({"none", "pyttsx3"})
