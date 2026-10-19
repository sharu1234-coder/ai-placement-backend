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
    assert body["detail"] == "This feature is temporarily unavailable on the server."
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
