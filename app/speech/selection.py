"""Choosing a speech provider, and refusing to choose a broken one.

Mirrors `app.ai.provider.get_ai_provider` on purpose -- same configuration style, same
framework-free module, same "raise on an unknown name rather than silently fall back" rule.
Two additions that the text path learned the hard way:

* `validate_declaration()` runs on construction, so a provider whose declared capabilities
  contradict its code cannot be returned at all.
* Fallback is **capability-gated**. A fallback that cannot do what the primary was asked to do
  is not a fallback; selecting it would convert a clear failure into a wrong answer, which is
  the worse outcome. `fallback_for` returns one only when the request is genuinely within the
  candidate's declared and validated capabilities.

No provider here is nominated as the production primary. That decision needs measured
evidence this environment cannot produce -- see `docs/speech-provider-evaluation.md`.
"""

from __future__ import annotations

from typing import Optional

from app.speech.contracts import (
    SpeechToText,
    SpeechUnsupported,
    SynthesizeRequest,
    TextToSpeech,
    TranscribeRequest,
)

#: Provider names accepted in configuration. "none" is explicit rather than implied by an
#: empty string: a deployment with no speech provider is a supported, testable state (the
#: App AI features do not need one), and it must be stated rather than inferred.
STT_PROVIDERS = frozenset({"none", "faster-whisper"})
TTS_PROVIDERS = frozenset({"none", "pyttsx3"})


class SpeechNotConfigured(SpeechUnsupported):
    """No speech provider is configured. A normal state, not a misconfiguration.

    The text-only features must keep working with voice unavailable, so this is raised at the
    point of use by whatever asked for speech, rather than at import or at startup.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(f"no {kind} provider is configured (set {kind.upper()}_PROVIDER)")


def get_speech_to_text(name: Optional[str] = None) -> SpeechToText:
    """The configured recogniser, validated.

    Raises `SpeechNotConfigured` when speech is switched off and `NotImplementedError` for an
    unrecognised name -- loudly, in the pattern `get_ai_provider` already uses, because a
    typo in configuration silently selecting a different provider is worse than a crash at
    startup.
    """
    from app.core.config import settings

    chosen = (name or settings.stt_provider).strip().lower()
    if chosen == "none":
        raise SpeechNotConfigured("stt")
    if chosen == "faster-whisper":
        from app.speech.faster_whisper_stt import FasterWhisperSpeechToText

        provider = FasterWhisperSpeechToText()
        provider.validate_declaration()
        return provider
    raise NotImplementedError(f"STT provider '{chosen}' is not implemented (known: {sorted(STT_PROVIDERS)})")


def get_text_to_speech(name: Optional[str] = None) -> TextToSpeech:
    from app.core.config import settings

    chosen = (name or settings.tts_provider).strip().lower()
    if chosen == "none":
        raise SpeechNotConfigured("tts")
    if chosen == "pyttsx3":
        from app.speech.pyttsx3_tts import Pyttsx3TextToSpeech

        provider = Pyttsx3TextToSpeech()
        provider.validate_declaration()
        return provider
    raise NotImplementedError(f"TTS provider '{chosen}' is not implemented (known: {sorted(TTS_PROVIDERS)})")


def stt_fallback_for(request: TranscribeRequest, candidates: list[SpeechToText]) -> Optional[SpeechToText]:
    """The first candidate that can genuinely serve `request`, or None.

    "Genuinely" means its own `check_supported` accepts the request. Asking the provider
    rather than comparing capability fields keeps one definition of "supported": a second
    comparison here could pass a request the provider then refuses, which is the drift this
    whole layer is built to avoid.
    """
    for candidate in candidates:
        try:
            candidate.validate_declaration()
            candidate.check_supported(request)
        except SpeechUnsupported:
            continue
        return candidate
    return None


def tts_fallback_for(request: SynthesizeRequest, candidates: list[TextToSpeech]) -> Optional[TextToSpeech]:
    """See `stt_fallback_for`."""
    for candidate in candidates:
        try:
            candidate.validate_declaration()
            candidate.check_supported(request)
        except SpeechUnsupported:
            continue
        return candidate
    return None
