"""The GitHub adapter: what it asks for, and how every way GitHub can fail is categorised.

Two themes run through this file.

The first is minimum permission. The authorization URL is asserted to carry no `scope` at all
and, in particular, never `public_repo` -- which reads like a public-repositories scope and is
actually read/WRITE on every public repository the student owns. A tool that only reads has no
business holding it, and a student should not be asked to grant it on the consent screen.

The second is that provider text never travels. Every failure below is asserted to arrive as a
category from ProviderConnection.ERROR_KINDS, and the messages are asserted not to contain the
body GitHub sent -- because whatever reaches the caller gets persisted to `last_error_kind` and
rendered to a student, and GitHub's bodies can echo back URLs, repository names and tokens.

httpx is stubbed rather than hit. These tests are about how responses are interpreted, and a
real network call would make them slow, flaky, and dependent on someone's rate limit.
"""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.config import settings
from app.services.professional_presence import errors
from app.services.professional_presence.providers.github import GitHubOAuthProvider

_TOKEN = "gho_NotARealTokenJustATestValue"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, raise_on_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._payload


@pytest.fixture()
def provider(monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "test-client-id")
    monkeypatch.setattr(settings, "github_client_secret", "test-client-secret")
    monkeypatch.setattr(
        settings,
        "github_oauth_redirect_uri",
        "https://api.example.com/professional-presence/connections/github/callback",
    )
    return GitHubOAuthProvider()


class TestAuthorizationUrl:
    def test_it_carries_the_pkce_challenge_with_s256(self, provider):
        url = provider.build_authorization_url("the-state", "the-challenge")
        params = parse_qs(urlparse(url).query)

        assert params["code_challenge"] == ["the-challenge"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["state"] == ["the-state"]
        assert params["client_id"] == ["test-client-id"]

    def test_it_requests_no_scope_at_all(self, provider):
        """The minimum-permission decision, pinned. GitHub grants read-only access to public
        profile and repository information with no scope, which is the whole V1 requirement."""
        params = parse_qs(urlparse(provider.build_authorization_url("s", "c")).query)
        assert "scope" not in params

    def test_it_never_requests_a_write_capable_scope(self, provider):
        """public_repo is read/WRITE despite the name. Asserted against the whole URL rather
        than the parsed scope, so it fails however the parameter gets introduced."""
        url = provider.build_authorization_url("s", "c")
        for forbidden in ("public_repo", "repo", "write:", "delete_repo", "admin:"):
            assert forbidden not in url

    def test_the_client_secret_never_appears_in_the_url(self, provider):
        """The authorization URL is handed to the app and opened in a browser. A secret here
        would be a secret in the student's history, and in any proxy between."""
        assert "test-client-secret" not in provider.build_authorization_url("s", "c")

    def test_it_points_at_the_configured_redirect_uri(self, provider):
        params = parse_qs(urlparse(provider.build_authorization_url("s", "c")).query)
        assert params["redirect_uri"] == [settings.github_oauth_redirect_uri]


class TestConfiguration:
    def test_unconfigured_without_both_halves(self, monkeypatch):
        monkeypatch.setattr(settings, "github_client_id", "id-only")
        monkeypatch.setattr(settings, "github_client_secret", None)
        assert GitHubOAuthProvider().is_configured() is False

    def test_configured_with_both(self, provider):
        assert provider.is_configured() is True


class TestExchangeCode:
    def test_a_successful_exchange_returns_the_credential(self, provider, monkeypatch):
        monkeypatch.setattr(
            httpx, "post", lambda *a, **k: _FakeResponse(200, {"access_token": _TOKEN, "scope": ""})
        )
        credential = provider.exchange_code("the-code", "the-verifier")

        assert credential.access_token == _TOKEN
        assert credential.granted_scopes == ""
        assert credential.refresh_token is None

    def test_the_verifier_and_secret_are_sent_in_the_body_not_the_url(self, provider, monkeypatch):
        """A query parameter lands in access logs and in GitHub's own request logging. The code,
        the verifier and the secret all belong in a POST body."""
        captured = {}

        def _post(url, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return _FakeResponse(200, {"access_token": _TOKEN})

        monkeypatch.setattr(httpx, "post", _post)
        provider.exchange_code("the-code", "the-verifier")

        assert "?" not in captured["url"]
        assert captured["data"]["code_verifier"] == "the-verifier"
        assert captured["data"]["client_secret"] == "test-client-secret"

    def test_it_asks_for_json(self, provider, monkeypatch):
        """Without Accept: application/json GitHub answers form-encoded, and every exchange --
        successful or not -- would be read as malformed."""
        captured = {}

        def _post(url, data=None, headers=None, timeout=None):
            captured["headers"] = headers
            return _FakeResponse(200, {"access_token": _TOKEN})

        monkeypatch.setattr(httpx, "post", _post)
        provider.exchange_code("c", "v")
        assert captured["headers"]["Accept"] == "application/json"

    def test_an_error_payload_on_http_200_is_unauthorized(self, provider, monkeypatch):
        """GitHub answers a bad, expired, already-used code -- or a verifier that does not match
        the challenge -- with HTTP 200 and an `error` key. Treating 200 as success is the classic
        way to accept a failed exchange."""
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: _FakeResponse(200, {"error": "bad_verification_code"}),
        )
        with pytest.raises(errors.ProviderUnauthorized):
            provider.exchange_code("c", "v")

    def test_the_github_error_string_is_not_repeated_back(self, provider, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: _FakeResponse(200, {"error": "bad_verification_code",
                                                "error_description": "secret-ish detail"}),
        )
        with pytest.raises(errors.ProviderUnauthorized) as caught:
            provider.exchange_code("c", "v")
        assert "bad_verification_code" not in str(caught.value)
        assert "secret-ish detail" not in str(caught.value)

    def test_a_missing_access_token_is_malformed(self, provider, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, {"token_type": "bearer"}))
        with pytest.raises(errors.ProviderMalformedResponse):
            provider.exchange_code("c", "v")

    def test_unparseable_json_is_malformed(self, provider, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, raise_on_json=True))
        with pytest.raises(errors.ProviderMalformedResponse):
            provider.exchange_code("c", "v")

    def test_a_json_array_where_an_object_is_documented_is_malformed(self, provider, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, ["unexpected"]))
        with pytest.raises(errors.ProviderMalformedResponse):
            provider.exchange_code("c", "v")

    def test_a_timeout_is_its_own_category(self, provider, monkeypatch):
        def _post(*a, **k):
            raise httpx.TimeoutException("timed out")

        monkeypatch.setattr(httpx, "post", _post)
        with pytest.raises(errors.ProviderTimeout):
            provider.exchange_code("c", "v")

    def test_a_connection_failure_is_unavailable(self, provider, monkeypatch):
        def _post(*a, **k):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(httpx, "post", _post)
        with pytest.raises(errors.ProviderUnavailable):
            provider.exchange_code("c", "v")


class TestStatusMapping:
    @pytest.mark.parametrize(
        "status_code,headers,expected",
        [
            (401, {}, errors.ProviderUnauthorized),
            (403, {}, errors.ProviderForbidden),
            (403, {"x-ratelimit-remaining": "0"}, errors.ProviderRateLimited),
            (429, {}, errors.ProviderRateLimited),
            (404, {}, errors.ProviderNotFound),
            (500, {}, errors.ProviderUnavailable),
            (502, {}, errors.ProviderUnavailable),
            (503, {}, errors.ProviderUnavailable),
        ],
    )
    def test_each_status_maps_to_its_category(self, provider, monkeypatch, status_code, headers, expected):
        """403 is split by header rather than by body text. GitHub uses it for both rate
        limiting and refusal, and the body is the only place it says which in prose -- prose this
        adapter has promised not to read into our state."""
        monkeypatch.setattr(
            httpx, "get", lambda *a, **k: _FakeResponse(status_code, {}, headers)
        )
        with pytest.raises(expected):
            provider.fetch_identity(_TOKEN)

    def test_every_category_is_a_known_error_kind(self):
        """The categories must be writable to provider_connections.last_error_kind without a
        translation table in between that could drift."""
        from app.models.provider_connection import ERROR_KINDS

        for cls in (
            errors.ProviderUnauthorized,
            errors.ProviderForbidden,
            errors.ProviderRateLimited,
            errors.ProviderNotFound,
            errors.ProviderTimeout,
            errors.ProviderUnavailable,
            errors.ProviderMalformedResponse,
        ):
            assert cls.kind in ERROR_KINDS


class TestFetchIdentity:
    def test_identity_uses_the_numeric_id_not_the_login(self, provider, monkeypatch):
        """A GitHub login can be renamed by its owner, after which the old name is available for
        somebody else to register. Keying ownership on it would let a connection silently follow
        whoever holds the name today."""
        monkeypatch.setattr(
            httpx, "get", lambda *a, **k: _FakeResponse(200, {"id": 583231, "login": "octocat"})
        )
        identity = provider.fetch_identity(_TOKEN)

        assert identity.provider_user_id == "583231"
        assert identity.username == "octocat"

    def test_it_sends_the_token_as_a_bearer_header(self, provider, monkeypatch):
        captured = {}

        def _get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse(200, {"id": 1, "login": "x"})

        monkeypatch.setattr(httpx, "get", _get)
        provider.fetch_identity(_TOKEN)

        assert captured["headers"]["Authorization"] == f"Bearer {_TOKEN}"
        # Never as a query parameter, where it would reach access logs.
        assert _TOKEN not in captured["url"]

    @pytest.mark.parametrize("payload", [{}, {"id": None}, {"id": "583231"}, {"login": "octocat"}])
    def test_an_id_that_is_not_a_number_is_malformed(self, provider, monkeypatch, payload):
        """Guessing here would mean binding a connection to an identity we cannot vouch for --
        including the string case, which would otherwise silently produce a different key space
        than the numeric one."""
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(200, payload))
        with pytest.raises(errors.ProviderMalformedResponse):
            provider.fetch_identity(_TOKEN)

    def test_a_missing_login_is_tolerated(self, provider, monkeypatch):
        """The username is display-only, so its absence must not fail a connection whose
        identity is perfectly well established."""
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(200, {"id": 7}))
        assert provider.fetch_identity(_TOKEN).username is None


class TestRevoke:
    def test_it_calls_the_grant_endpoint_with_the_app_credentials(self, provider, monkeypatch):
        captured = {}

        def _request(method, url, auth=None, json=None, headers=None, timeout=None):
            captured.update(method=method, url=url, auth=auth, json=json)
            return _FakeResponse(204, {})

        monkeypatch.setattr(httpx, "request", _request)
        provider.revoke(_TOKEN)

        assert captured["method"] == "DELETE"
        assert captured["url"].endswith("/applications/test-client-id/grant")
        assert captured["auth"] == ("test-client-id", "test-client-secret")
        assert captured["json"] == {"access_token": _TOKEN}

    def test_a_provider_failure_does_not_raise(self, provider, monkeypatch):
        """Disconnection must succeed even when GitHub is unreachable. Raising here would leave
        a student unable to disconnect during an outage, which is the wrong way for that
        failure to fall -- our copy of the token is destroyed by the caller either way."""
        def _request(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(httpx, "request", _request)
        provider.revoke(_TOKEN)  # must not raise

    def test_it_does_nothing_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(settings, "github_client_id", None)
        monkeypatch.setattr(settings, "github_client_secret", None)

        def _request(*a, **k):
            raise AssertionError("should not have called GitHub")

        monkeypatch.setattr(httpx, "request", _request)
        GitHubOAuthProvider().revoke(_TOKEN)
