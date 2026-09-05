import httpx
import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.project_builder import GeneratedStageGuidanceSet
from app.main import app


class FakeAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        if schema is GeneratedStageGuidanceSet:
            return GeneratedStageGuidanceSet(stages=[])
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def use_fake_ai():
    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "ghcheck@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "ghcheck@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "ghcheck2@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "ghcheck2@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _repo_response(status_code=200, **overrides):
    body = {"language": "Python", "stargazers_count": 5, "pushed_at": "2026-08-01T12:00:00Z", **overrides}
    return httpx.Response(status_code, json=body, request=httpx.Request("GET", "https://api.github.com/repos/x/y"))


def test_requires_auth(client):
    resp = client.post("/project-builder/1/github-check", json={"repo_url": "https://github.com/o/r"})
    assert resp.status_code == 401


def test_successful_check_returns_evidence_and_is_reflected_on_the_build(client, auth_headers, use_fake_ai, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _repo_response())
    created = client.post(
        "/project-builder", json={"title": "Project", "stack_summary": "Python"}, headers=auth_headers
    )
    build_id = created.json()["id"]

    resp = client.post(
        f"/project-builder/{build_id}/github-check",
        json={"repo_url": "https://github.com/octocat/Hello-World"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["repo_exists"] is True
    assert body["primary_language"] == "Python"
    assert body["matches_declared_stack"] is True

    build = client.get(f"/project-builder/{build_id}", headers=auth_headers).json()
    assert build["github_evidence"]["repo_exists"] is True


def test_invalid_url_returns_422(client, auth_headers, use_fake_ai):
    build_id = client.post("/project-builder", json={"title": "Project"}, headers=auth_headers).json()["id"]

    resp = client.post(
        f"/project-builder/{build_id}/github-check", json={"repo_url": "not a url"}, headers=auth_headers
    )
    assert resp.status_code == 422


def test_rate_limited_returns_503(client, auth_headers, use_fake_ai, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _repo_response(status_code=403))
    build_id = client.post("/project-builder", json={"title": "Project"}, headers=auth_headers).json()["id"]

    resp = client.post(
        f"/project-builder/{build_id}/github-check",
        json={"repo_url": "https://github.com/octocat/Hello-World"},
        headers=auth_headers,
    )
    assert resp.status_code == 503


def test_unknown_build_returns_404(client, auth_headers, use_fake_ai):
    resp = client.post(
        "/project-builder/999999/github-check",
        json={"repo_url": "https://github.com/octocat/Hello-World"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_cannot_check_another_profiles_build(client, auth_headers, other_auth_headers, use_fake_ai):
    build_id = client.post("/project-builder", json={"title": "Mine"}, headers=auth_headers).json()["id"]

    resp = client.post(
        f"/project-builder/{build_id}/github-check",
        json={"repo_url": "https://github.com/octocat/Hello-World"},
        headers=other_auth_headers,
    )
    assert resp.status_code == 404
