import httpx
import pytest

from app.services import github_verification_service as svc

REPO_URL = "https://github.com/octocat/Hello-World"


def _repo_response(status_code=200, **overrides):
    body = {
        "language": "Python",
        "stargazers_count": 42,
        "pushed_at": "2026-08-01T12:00:00Z",
        **overrides,
    }
    return httpx.Response(status_code, json=body, request=httpx.Request("GET", "https://api.github.com/repos/x/y"))


def _readme_response(status_code=200, text="# Hello World\n\nA sample repo."):
    return httpx.Response(status_code, text=text, request=httpx.Request("GET", "https://api.github.com/x"))


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octocat/Hello-World",
        "https://github.com/octocat/Hello-World.git",
        "https://github.com/octocat/Hello-World/",
        "http://github.com/octocat/Hello-World",
    ],
)
def test_parse_repo_url_accepts_common_variants(url):
    owner, repo = svc.parse_repo_url(url)
    assert owner == "octocat"
    assert repo == "Hello-World"


@pytest.mark.parametrize("url", ["not a url", "https://gitlab.com/owner/repo", "https://github.com/owner", ""])
def test_parse_repo_url_rejects_invalid_input(url):
    with pytest.raises(svc.InvalidRepoUrlError):
        svc.parse_repo_url(url)


def test_repo_not_found_returns_a_clean_not_found_result(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _repo_response(status_code=404))

    evidence = svc.check_repository(REPO_URL)

    assert evidence.repo_exists is False
    assert "no public repository" in evidence.evidence_label.lower()


def test_rate_limit_raises_github_api_error_not_a_false_not_found(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _repo_response(status_code=403))

    with pytest.raises(svc.GitHubApiError):
        svc.check_repository(REPO_URL)


def test_unexpected_status_raises_github_api_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _repo_response(status_code=500))

    with pytest.raises(svc.GitHubApiError):
        svc.check_repository(REPO_URL)


def test_network_failure_raises_github_api_error(monkeypatch):
    def raise_error(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", raise_error)

    with pytest.raises(svc.GitHubApiError):
        svc.check_repository(REPO_URL)


def test_repo_found_with_readme_and_matching_language(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/readme"):
            return _readme_response()
        return _repo_response()

    monkeypatch.setattr(httpx, "get", fake_get)

    evidence = svc.check_repository(REPO_URL, stack_summary="Python, Docker")

    assert evidence.repo_exists is True
    assert evidence.primary_language == "Python"
    assert evidence.has_readme is True
    assert evidence.readme_snippet is not None
    assert evidence.stars == 42
    assert evidence.matches_declared_stack is True
    assert evidence.last_pushed_at is not None
    assert "well-documented" in evidence.evidence_label.lower()


def test_repo_found_without_readme(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/readme"):
            return _readme_response(status_code=404)
        return _repo_response()

    monkeypatch.setattr(httpx, "get", fake_get)

    evidence = svc.check_repository(REPO_URL)

    assert evidence.has_readme is False
    assert evidence.readme_snippet is None
    assert "no readme" in evidence.evidence_label.lower()


def test_readme_fetch_failure_does_not_block_the_main_result(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/readme"):
            raise httpx.ConnectError("readme unreachable")
        return _repo_response()

    monkeypatch.setattr(httpx, "get", fake_get)

    evidence = svc.check_repository(REPO_URL)

    assert evidence.repo_exists is True
    assert evidence.has_readme is False


def test_matches_declared_stack_is_none_without_a_declared_stack(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/readme"):
            return _readme_response(status_code=404)
        return _repo_response()

    monkeypatch.setattr(httpx, "get", fake_get)

    evidence = svc.check_repository(REPO_URL, stack_summary=None)
    assert evidence.matches_declared_stack is None


def test_matches_declared_stack_is_none_when_repo_has_no_detected_language(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/readme"):
            return _readme_response(status_code=404)
        return _repo_response(language=None)

    monkeypatch.setattr(httpx, "get", fake_get)

    evidence = svc.check_repository(REPO_URL, stack_summary="Python, Docker")
    assert evidence.matches_declared_stack is None


def test_matches_declared_stack_is_false_for_a_genuine_mismatch(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/readme"):
            return _readme_response(status_code=404)
        return _repo_response(language="Ruby")

    monkeypatch.setattr(httpx, "get", fake_get)

    evidence = svc.check_repository(REPO_URL, stack_summary="Python, Docker")
    assert evidence.matches_declared_stack is False
