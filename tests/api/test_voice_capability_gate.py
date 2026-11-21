"""VA-0.5 item 0.5-1 at route level: a voice-incapable provider is refused up front.

The unit tests in tests/unit/test_provider_capabilities.py prove the capability logic. This
proves the thing a student would actually experience, and the three properties that make the
fix worth having:

  * The refusal is a 503, not a 500 -- the condition is a server misconfiguration, and
    nothing about the request is wrong.
  * It happens before any work: no upload is read, no question is looked up, no model call.
  * The provider name and the missing capability do not reach the client, which could
    otherwise be probed by anyone able to trigger the failure.

No provider override here, deliberately: this test selects the real AnthropicProvider through
the real `get_ai_provider` by setting AI_PROVIDER, because the bug being fixed was in
selection. The API key is a dummy and no network call is made -- the request is refused
before the client is used.
"""

import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.api.capabilities import speech_capabilities
from app.core.config import settings
from app.database.base import Base
from app.main import app
from tests.fixtures import replay_harness as h


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "va05_capability.db")
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def anthropic_client(engine, tmp_path, monkeypatch):
    """A client whose configured provider is Anthropic, which cannot do speech."""
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "not-a-real-key")
    client, sessions = h.make_client(engine, tmp_path, monkeypatch)
    with client as test_client:
        yield test_client, sessions
    app.dependency_overrides.clear()


def test_submitting_an_audio_answer_is_refused_with_503(anthropic_client):
    client, _sessions = anthropic_client
    headers = h.signup(client, "cap-audio@example.com")

    audio = bytes([0, 0, 0, 0x18]) + b"ftypM4A " + b"\x00" * 64
    response = client.post(
        "/interviews/1/answer/audio",
        headers=headers,
        data={"interview_question_id": "1"},
        files={"file": ("answer.m4a", audio, "audio/mp4")},
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["detail"] == "Voice is not available on this server. You can type your answer instead."
    # The diagnostic belongs in the logs, not in a response anyone can trigger.
    for leaked in ("anthropic", "transcribe", "AI_PROVIDER", "capability"):
        assert leaked.lower() not in response.text.lower(), f"response leaked '{leaked}'"


def test_fetching_question_audio_is_refused_with_503(anthropic_client):
    client, _sessions = anthropic_client
    headers = h.signup(client, "cap-tts@example.com")

    response = client.get("/interviews/1/questions/1/audio", headers=headers)

    assert response.status_code == 503, response.text
    assert "anthropic" not in response.text.lower()


def test_the_refusal_happens_before_the_interview_is_even_looked_up(anthropic_client):
    """Interview 999999 does not exist.

    A 404 here would mean the route body ran -- i.e. the capability check had been pushed
    past the ownership lookup, which is the old behaviour in a new place. 503 means the
    request never got that far.
    """
    client, _sessions = anthropic_client
    headers = h.signup(client, "cap-order@example.com")

    response = client.get("/interviews/999999/questions/1/audio", headers=headers)
    assert response.status_code == 503, (
        f"expected refusal at selection, got {response.status_code}: the capability check is "
        "running too late"
    )


def test_an_unauthenticated_request_is_still_rejected_as_unauthenticated(anthropic_client):
    """The capability gate must not become an authentication bypass or an info leak.

    Ordering matters: an unauthenticated caller should learn nothing about the server's AI
    configuration, so this must be a 401/403 rather than the 503.
    """
    client, _sessions = anthropic_client
    response = client.get("/interviews/1/questions/1/audio")
    assert response.status_code in (401, 403), response.text


def test_the_text_routes_still_work_with_a_text_only_provider(anthropic_client):
    """The control, and the point of declaring capabilities per operation rather than per
    provider: Anthropic cannot do speech, but it must remain fully usable for everything else.

    Listing interviews needs no AI at all, so it must be unaffected by the gate.
    """
    client, _sessions = anthropic_client
    headers = h.signup(client, "cap-text@example.com")

    listed = client.get("/interviews/", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == []


# ----------------------------------------------------------------------------------------
# H2/H5 -- the client has to be able to ask BEFORE it offers a voice control.
#
# Everything above proves the refusal is correct. None of it helped the student, because the
# refusal arrives after they chose to speak and the Android client rendered a 503 as a generic
# retryable server error. These cover the contract that moves the answer earlier, and the
# machine-readable marker that stops the retry loop for a client that asked anyway.
# ----------------------------------------------------------------------------------------


class _SpeechCapableProvider(AIProvider):
    """Overrides both voice methods, so __init_subclass__ derives TRANSCRIBE and SYNTHESIZE.

    Deliberately a real AIProvider subclass rather than a stub with a hand-written capability
    set: the capability set is derived from which methods a class actually overrides, and a
    double that declared its own would be able to be wrong about itself in exactly the way the
    production bug was.
    """

    name = "speech-capable-double"

    def generate_structured(self, prompt, schema):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def generate_text(self, prompt: str) -> str:  # pragma: no cover - not exercised here
        raise NotImplementedError

    def transcribe(self, audio_bytes: bytes) -> str:  # pragma: no cover
        return "a transcript"

    def synthesize_speech(self, text: str) -> bytes:  # pragma: no cover
        return b"audio"


def test_the_refusal_carries_a_code_that_is_not_a_transient_outage(anthropic_client):
    """A 503 alone is indistinguishable from the server being briefly down, and the client's
    shared classifier maps every 5xx to a retryable error. The code is what lets it tell a
    settled refusal from an outage without parsing the message text."""
    client, _sessions = anthropic_client
    headers = h.signup(client, "cap-code@example.com")

    response = client.get("/interviews/1/questions/1/audio", headers=headers)

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "speech_not_supported"
    # The old copy said "temporarily unavailable", which invited the retry it then got.
    assert "temporarily" not in response.text.lower()
    for leaked in ("anthropic", "transcribe", "synthesize", "AI_PROVIDER", "capability"):
        assert leaked.lower() not in response.text.lower(), f"response leaked '{leaked}'"


def test_speech_capabilities_reports_no_speech_for_the_production_provider(anthropic_client, monkeypatch):
    """The production shape: AI_PROVIDER=anthropic and no speech provider selected.

    STT_PROVIDER is pinned to "none" rather than inherited, because this developer machine's
    .env configures the development-only faster-whisper recogniser -- so the ambient value here
    is True, and a test that relied on it would assert the opposite of the production state it
    is named after.
    """
    client, _sessions = anthropic_client
    monkeypatch.setattr(settings, "stt_provider", "none")
    headers = h.signup(client, "cap-report@example.com")

    response = client.get("/capabilities/speech", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"transcribe": False, "synthesize": False, "realtime_voice": False}


def test_speech_capabilities_leaks_nothing_about_how_the_server_is_built(anthropic_client):
    client, _sessions = anthropic_client
    headers = h.signup(client, "cap-noleak@example.com")

    response = client.get("/capabilities/speech", headers=headers)

    for leaked in ("anthropic", "ollama", "whisper", "pyttsx3", "api_key", "provider"):
        assert leaked.lower() not in response.text.lower(), f"response leaked '{leaked}'"


def test_speech_capabilities_requires_authentication(anthropic_client):
    """Not a secret, but an unauthenticated probe of a deployment's AI configuration is free
    reconnaissance and nothing needs this before login."""
    client, _sessions = anthropic_client

    response = client.get("/capabilities/speech")

    assert response.status_code in (401, 403), response.text


def test_speech_capabilities_reports_the_real_capabilities_of_a_capable_provider(anthropic_client):
    """The other direction, so "always false" cannot pass this file.

    When a provider that genuinely implements both voice methods is configured, the contract
    must say so -- otherwise the text-first launch policy would become permanent in code.
    """
    client, _sessions = anthropic_client
    headers = h.signup(client, "cap-capable@example.com")
    app.dependency_overrides[get_ai_provider] = _SpeechCapableProvider

    try:
        response = client.get("/capabilities/speech", headers=headers)
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcribe"] is True
    assert body["synthesize"] is True


def test_realtime_voice_is_reported_only_when_a_recogniser_is_configured(monkeypatch):
    """Realtime voice is gated by STT_PROVIDER in the separate speech layer, not by the AI
    provider, so it is reported from there. An unrecognised name is not a configured
    recogniser -- the same rule get_speech_to_text applies."""
    provider = _SpeechCapableProvider()

    monkeypatch.setattr(settings, "stt_provider", "none")
    assert speech_capabilities(provider).realtime_voice is False

    monkeypatch.setattr(settings, "stt_provider", "faster-whisper")
    assert speech_capabilities(provider).realtime_voice is True

    monkeypatch.setattr(settings, "stt_provider", "a-typo-nobody-implements")
    assert speech_capabilities(provider).realtime_voice is False


def test_a_text_only_provider_reports_no_speech_from_the_pure_mapping():
    """The mapping itself, with no HTTP and no configuration ambiguity.

    The fail-safe for an *unreadable* contract deliberately lives on the client, not here: a
    backend that predates this route answers 404, which no server-side handling can cover, so
    the client has to default to typing on any failure anyway. Duplicating that here would
    only hide a misconfiguration from the operator. See SpeechCapabilitiesTest on Android.
    """
    from app.ai.anthropic_provider import AnthropicProvider

    capabilities = speech_capabilities(AnthropicProvider.__new__(AnthropicProvider))

    assert capabilities.transcribe is False
    assert capabilities.synthesize is False
