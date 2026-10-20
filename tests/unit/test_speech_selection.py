"""Tier 1: provider selection, and the two things selection must never do.

It must not return a provider whose declaration contradicts its code, and it must not fall
back to one that cannot do what was asked. The second is the subtler of the two: a fallback
that silently accepts a request it cannot serve converts a clear, typed failure into a wrong
answer, and a wrong transcript is far more expensive than a refused one.

Also asserted here: the text-only features keep working with no speech provider configured.
That is a supported deployment, not a broken one.
"""

import pytest

from app.speech.contracts import (
    REALTIME_PCM16,
    AudioEncoding,
    AudioFormat,
    Speech,
    SpeechToText,
    SpeechUnsupported,
    SttCapabilities,
    SynthesizeRequest,
    TextToSpeech,
    TranscribeRequest,
    Transcript,
    TtsCapabilities,
)
from app.speech.selection import (
    STT_PROVIDERS,
    TTS_PROVIDERS,
    SpeechNotConfigured,
    get_speech_to_text,
    get_text_to_speech,
    stt_fallback_for,
    tts_fallback_for,
)


class EnglishOnlyStt(SpeechToText):
    name = "english-only"

    @property
    def capabilities(self):
        return SttCapabilities(
            input_formats=frozenset({AudioEncoding.PCM16}),
            sample_rates_hz=frozenset({16_000}),
            max_channel_count=1,
            languages=frozenset({"en"}),
            max_audio_seconds=30.0,
            supplies_confidence=False,
        )

    def transcribe(self, request):
        self.check_supported(request)
        return Transcript(text="english", is_final=True)


class MultilingualStt(SpeechToText):
    name = "multilingual"

    @property
    def capabilities(self):
        return SttCapabilities(
            input_formats=frozenset({AudioEncoding.PCM16}),
            sample_rates_hz=frozenset({16_000, 48_000}),
            max_channel_count=2,
            languages=frozenset(),  # detects
            max_audio_seconds=120.0,
            supplies_confidence=True,
        )

    def transcribe(self, request):
        self.check_supported(request)
        return Transcript(text="any language", is_final=True)


class LyingStt(EnglishOnlyStt):
    name = "lying"

    @property
    def capabilities(self):
        base = super().capabilities
        return SttCapabilities(
            input_formats=base.input_formats,
            sample_rates_hz=base.sample_rates_hz,
            max_channel_count=base.max_channel_count,
            languages=base.languages,
            max_audio_seconds=base.max_audio_seconds,
            supplies_confidence=base.supplies_confidence,
            streaming=True,  # not implemented
        )


def pcm(seconds: float = 1.0, rate: int = 16_000, channels: int = 1) -> bytes:
    return b"\x00\x01" * int(seconds * rate) * channels


class TestConfiguredSelection:
    def test_none_means_speech_is_switched_off_not_broken(self):
        with pytest.raises(SpeechNotConfigured):
            get_speech_to_text("none")
        with pytest.raises(SpeechNotConfigured):
            get_text_to_speech("none")

    def test_that_is_the_default(self, monkeypatch):
        # No provider has been measured well enough to be a production default, so the default
        # must be the honest one.
        #
        # This asserts the *declared* default, which is the claim being made, and deliberately
        # does not read `app.core.config.settings`. That singleton is whatever this machine is
        # configured to run: pydantic fills it from the process environment and from `.env`,
        # and a developer with `TTS_PROVIDER=pyttsx3` in their `.env` is correctly configured,
        # not in breach of this contract. Reading the singleton made the test pass or fail on
        # the contents of an untracked file: measured, it passed wherever `.env` was not
        # discoverable and failed from a repository root with a populated `.env` -- the worst
        # of both, since it never caught a real default change and failed for a reason that
        # was not a bug. (No claim is made about CI here: no workflow has run on this branch.)
        from app.core.config import Settings

        assert Settings.model_fields["stt_provider"].default == "none"
        assert Settings.model_fields["tts_provider"].default == "none"

        # And a process with nothing configured must actually get those defaults -- the field
        # declaration above would not catch a validator or an alias that rewrote the value on
        # the way in. `_env_file=None` ignores `.env`; the two `delenv` calls cover the
        # process-environment overrides that would otherwise win over the default.
        monkeypatch.delenv("STT_PROVIDER", raising=False)
        monkeypatch.delenv("TTS_PROVIDER", raising=False)
        controlled = Settings(_env_file=None)
        assert controlled.stt_provider == "none"
        assert controlled.tts_provider == "none"

    def test_an_unknown_name_raises_rather_than_falling_back(self):
        # A typo in configuration silently selecting a different provider is worse than a
        # crash: nobody would notice until the quality complaints arrived.
        with pytest.raises(NotImplementedError):
            get_speech_to_text("whisper-large-v9")
        with pytest.raises(NotImplementedError):
            get_text_to_speech("elevenlabs")

    def test_the_known_names_are_the_ones_that_can_be_built(self):
        assert STT_PROVIDERS == frozenset({"none", "faster-whisper"})
        assert TTS_PROVIDERS == frozenset({"none", "pyttsx3"})

    def test_names_are_case_and_whitespace_tolerant(self):
        with pytest.raises(SpeechNotConfigured):
            get_speech_to_text("  NONE ")

    def test_a_built_provider_is_validated_on_the_way_out(self):
        provider = get_speech_to_text("faster-whisper")
        assert provider.name == "faster-whisper"
        provider.validate_declaration()
        # Construction must not load the model: selection has to stay cheap enough to happen
        # per request, and a provider that downloads weights on import cannot be.
        assert provider._model is None


class TestCapabilityGatedFallback:
    def test_the_first_capable_candidate_wins(self):
        request = TranscribeRequest(pcm(), REALTIME_PCM16, language="en")
        assert stt_fallback_for(request, [EnglishOnlyStt(), MultilingualStt()]).name == "english-only"

    def test_a_candidate_that_cannot_serve_the_request_is_skipped(self):
        hindi = TranscribeRequest(pcm(), REALTIME_PCM16, language="hi")
        assert stt_fallback_for(hindi, [EnglishOnlyStt(), MultilingualStt()]).name == "multilingual"

    def test_no_capable_candidate_returns_none_rather_than_the_closest_one(self):
        # "Closest" is how a 48 kHz stereo stream reaches a mono 16 kHz recogniser.
        too_long = TranscribeRequest(pcm(600), REALTIME_PCM16, language="en")
        assert stt_fallback_for(too_long, [EnglishOnlyStt(), MultilingualStt()]) is None

    def test_a_candidate_with_an_inconsistent_declaration_is_never_chosen(self):
        request = TranscribeRequest(pcm(), REALTIME_PCM16, language="en")
        assert stt_fallback_for(request, [LyingStt()]) is None
        assert stt_fallback_for(request, [LyingStt(), EnglishOnlyStt()]).name == "english-only"

    def test_the_rate_gate_is_real(self):
        request = TranscribeRequest(pcm(1, 48_000, 2), AudioFormat(AudioEncoding.PCM16, 48_000, 2))
        assert stt_fallback_for(request, [EnglishOnlyStt()]) is None
        assert stt_fallback_for(request, [MultilingualStt()]).name == "multilingual"


class EnglishTts(TextToSpeech):
    name = "english-tts"

    @property
    def capabilities(self):
        return TtsCapabilities(
            output_format=AudioFormat(AudioEncoding.WAV, 22_050, 1),
            languages=frozenset({"en"}),
            max_characters=100,
        )

    def synthesize(self, request):
        self.check_supported(request)
        return Speech(audio=b"RIFF", audio_format=self.capabilities.output_format)


class WideTts(EnglishTts):
    name = "wide-tts"

    @property
    def capabilities(self):
        return TtsCapabilities(
            output_format=AudioFormat(AudioEncoding.WAV, 22_050, 1),
            languages=frozenset({"en", "hi"}),
            max_characters=5_000,
        )


class TestTtsFallback:
    def test_a_language_the_first_candidate_cannot_speak_moves_on(self):
        request = SynthesizeRequest(text="namaste", language="hi")
        assert tts_fallback_for(request, [EnglishTts(), WideTts()]).name == "wide-tts"

    def test_text_too_long_for_every_candidate_returns_none(self):
        request = SynthesizeRequest(text="x" * 10_000)
        assert tts_fallback_for(request, [EnglishTts(), WideTts()]) is None

    def test_a_servable_request_takes_the_first(self):
        assert tts_fallback_for(SynthesizeRequest(text="hello"), [EnglishTts(), WideTts()]).name == "english-tts"


class TestTextFeaturesSurviveWithoutSpeech:
    def test_the_text_provider_is_unaffected_by_speech_configuration(self):
        # The speech layer is additive. Selecting a text provider must not import it, let
        # alone require one to be configured.
        from app.ai.provider import AICapability, get_ai_provider

        provider = get_ai_provider()
        assert AICapability.TEXT in provider.capabilities

    def test_nothing_in_the_ai_layer_imports_the_speech_layer(self):
        # Asserted structurally rather than trusted: an import added later would make the
        # text features depend on a speech provider being installable, which is the
        # regression this test exists to catch.
        import pathlib
        import re

        imports = re.compile(r"^\s*(?:import|from)\s+app\.speech\b", re.MULTILINE)
        ai_dir = pathlib.Path(__file__).resolve().parents[2] / "app" / "ai"
        offenders = [
            path.name for path in ai_dir.glob("*.py") if imports.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_the_speech_layer_does_not_import_fastapi(self):
        # Same rule the AI provider module follows: services import these contracts, and they
        # must not acquire a web framework on the way.
        import pathlib
        import re

        # Import statements, not prose: these modules discuss the rule in their docstrings,
        # and a substring search would fail on the comment explaining why it holds.
        imports = re.compile(r"^\s*(?:import|from)\s+fastapi\b", re.MULTILINE)
        speech_dir = pathlib.Path(__file__).resolve().parents[2] / "app" / "speech"
        offenders = [
            path.name for path in speech_dir.glob("*.py") if imports.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []


class TestSpeechHasNoAuthority:
    """A speech provider converts signals. It must not be able to read a user or change one.

    This is a structural guarantee rather than a convention, because the consequence of losing
    it is specific and bad: if a recogniser could reach the database or a service, a
    mis-transcription would become an action. "Book the interview" and "cancel the interview"
    differ by one word that an ASR model can get wrong.
    """

    @pytest.mark.parametrize("forbidden", ["app.models", "app.services", "app.api", "app.database", "sqlalchemy"])
    def test_the_speech_layer_cannot_reach_data_or_mutations(self, forbidden):
        import pathlib
        import re

        pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}\b", re.MULTILINE)
        speech_dir = pathlib.Path(__file__).resolve().parents[2] / "app" / "speech"
        offenders = [
            path.name for path in speech_dir.glob("*.py") if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"{offenders} import {forbidden}; a speech provider must not have that reach"

    def test_the_only_app_dependency_is_configuration(self):
        # app.core.config is the one exception, and only for a model name and a provider
        # choice. Worth pinning: it is the import that would make a wider dependency look
        # ordinary later.
        #
        # app.core.voice_selection is the second, added deliberately. It holds the one voice
        # selector that BOTH synthesis paths call, and it cannot live in app/speech because
        # test_nothing_in_the_ai_layer_imports_the_speech_layer (above) forbids app/ai from
        # importing this layer -- and the two paths having separate selectors is precisely
        # the defect that let the Voice Career Coach speak in the host default while
        # configuration, code and tests all said "female". The module is stdlib-only with a
        # single lazy read of app.core.config, so it brings no dependency with it.
        #
        # Still an exact set, so anything unplanned still fails here.
        import pathlib
        import re

        speech_dir = pathlib.Path(__file__).resolve().parents[2] / "app" / "speech"
        imported = set()
        for path in speech_dir.glob("*.py"):
            imported.update(re.findall(r"^\s*from\s+(app\.[\w.]+)", path.read_text(encoding="utf-8"), re.MULTILINE))
        outside_the_layer = {name for name in imported if not name.startswith("app.speech")}
        assert outside_the_layer == {
            "app.core.config",
            "app.core.voice_selection",
        }, outside_the_layer


class TestUnsupportedIsAlwaysSpeechUnsupported:
    def test_every_pre_execution_refusal_shares_one_base_class(self):
        # So a caller can write one `except SpeechUnsupported` for "this provider cannot do
        # it" and keep that distinct from a transient failure it may retry.
        from app.speech.contracts import (
            UnsupportedAudioFormat,
            UnsupportedLanguage,
            UnsupportedOperation,
        )

        for cls in (UnsupportedAudioFormat, UnsupportedLanguage, UnsupportedOperation, SpeechNotConfigured):
            assert issubclass(cls, SpeechUnsupported)
