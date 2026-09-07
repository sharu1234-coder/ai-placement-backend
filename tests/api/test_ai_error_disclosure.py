"""The AI provider's own error text must never reach an API client.

The defect this covers. Six routes (assessments x2, interviews x2, roadmap x2) turned an
AIGenerationError into `HTTPException(422, detail=str(exc))`. That looks harmless until you
read how the message is built: app/ai/anthropic_provider.py interpolates the provider's error
into it --

    raise AIGenerationError(f"Anthropic API call failed ({exc.status_code}): {exc.message}")
    raise AIGenerationError(f"Could not reach the Anthropic API: {exc}")

-- so a bad key would have shown every student a 422 body reading "Anthropic API call failed
(401): invalid x-api-key", and a network fault could surface internal host or proxy detail the
same way. It also names the AI vendor to anyone able to trigger a failure.

The fix routes all six through app/api/ai_errors.ai_unavailable, which logs the real reason
server-side (findable by request id) and returns one fixed sentence. These tests assert the
client-facing half of that contract, because that is the half an attacker sees.
"""

import pytest

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.main import app

# Deliberately shaped like a real provider failure, with the kind of detail that must not
# escape: a vendor name, an HTTP status, an internal hostname and a credential hint.
_LEAKY_PROVIDER_MESSAGE = (
    "Anthropic API call failed (401): invalid x-api-key for org org_abc123 "
    "via internal-proxy.svc.cluster.local:8443"
)


class _FailingAI(AIProvider):
    def generate_structured(self, prompt, schema):
        raise AIGenerationError(_LEAKY_PROVIDER_MESSAGE)

    def generate_text(self, prompt):
        raise AIGenerationError(_LEAKY_PROVIDER_MESSAGE)

    def transcribe(self, audio: bytes) -> str:
        raise AIGenerationError(_LEAKY_PROVIDER_MESSAGE)

    def synthesize_speech(self, text: str) -> bytes:
        raise AIGenerationError(_LEAKY_PROVIDER_MESSAGE)


@pytest.fixture()
def failing_ai():
    app.dependency_overrides[get_ai_provider] = lambda: _FailingAI()
    yield
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def auth_headers(client):
    client.post(
        "/auth/signup", json={"email": "aierr@example.com", "password": "supersecret123"}
    )
    token = client.post(
        "/auth/login", json={"email": "aierr@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def sde_role_id(client):
    return next(r["id"] for r in client.get("/roles").json() if r["name"] == "SDE")


def _assert_no_disclosure(resp):
    assert resp.status_code == 422, resp.text
    body = resp.text
    for secret in (
        "Anthropic",
        "x-api-key",
        "org_abc123",
        "internal-proxy.svc.cluster.local",
        "401",
    ):
        assert secret not in body, f"provider detail {secret!r} leaked to the client: {body}"
    assert "temporarily unavailable" in resp.json()["detail"]


def test_assessment_question_generation_failure_does_not_disclose_provider_detail(
    client, auth_headers, sde_role_id, failing_ai
):
    blueprint_id = client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=auth_headers
    ).json()["id"]
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]

    _assert_no_disclosure(
        client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers)
    )


def test_interview_question_generation_failure_does_not_disclose_provider_detail(
    client, auth_headers, sde_role_id, failing_ai
):
    client.post("/users/me/role", json={"role_id": sde_role_id}, headers=auth_headers)
    interview_id = client.post("/interviews", json={}, headers=auth_headers).json()["id"]

    # This route had NO AIGenerationError handler at all until this pass, so a provider outage
    # mid-interview escaped as an unhandled 500 rather than the 422 its assessments
    # counterpart returns. Found by this very test, then confirmed across every AI-taking
    # route: /jobs and /interviews/{id}/evaluation had the same gap.
    _assert_no_disclosure(
        client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers)
    )


def test_job_description_parsing_failure_returns_422_not_500(
    client, auth_headers, failing_ai
):
    """JD parsing has no template fallback in its service -- there is nothing sensible to
    invent from an unparsed job description -- so the error reaches the route. It was the one
    route of twenty still answering 500 when the provider failed."""
    _assert_no_disclosure(
        client.post("/jobs", json={"raw_text": "a job description"}, headers=auth_headers)
    )


def test_roadmap_generation_failure_does_not_disclose_provider_detail(
    client, auth_headers, sde_role_id, failing_ai
):
    resp = client.post("/roadmap/generate", json={}, headers=auth_headers)
    assert "Anthropic" not in resp.text
    assert "x-api-key" not in resp.text
    assert "internal-proxy.svc.cluster.local" not in resp.text


def test_the_generic_message_is_the_same_regardless_of_the_underlying_failure(
    client, auth_headers, sde_role_id
):
    """If the text varied with the cause, the variation would itself be the disclosure."""
    import logging

    from app.api.ai_errors import ai_unavailable

    logger = logging.getLogger("test")
    first = ai_unavailable(AIGenerationError("invalid x-api-key"), logger, "assessments")
    second = ai_unavailable(AIGenerationError("connection refused to 10.0.0.5"), logger, "roadmap")
    assert first.detail == second.detail
    assert "x-api-key" not in first.detail
    assert "10.0.0.5" not in second.detail


def test_the_real_reason_is_still_logged_for_operators(caplog):
    """The detail must survive somewhere -- it just belongs in the log, not the response."""
    import logging

    logger = logging.getLogger("app.api.test_ai_errors")
    from app.api.ai_errors import ai_unavailable

    with caplog.at_level(logging.WARNING, logger="app.api.test_ai_errors"):
        ai_unavailable(AIGenerationError(_LEAKY_PROVIDER_MESSAGE), logger, "assessments")

    assert "invalid x-api-key" in caplog.text
    assert "assessments" in caplog.text
