"""The GitHub OAuth flow end to end, and every way it must refuse.

The callback is the route worth being careful about: it is reachable with no bearer token and no
App Check header, because GitHub's redirect carries neither. That is deliberate and follows the
precedent POST /subscriptions/webhook already set for Razorpay -- but it means the state is the
entire access control for the route, so most of this file is about what happens when the state
is wrong.

The single most important assertion here is `test_the_redirect_back_into_the_app_carries_no_secret`.
A custom URL scheme can be registered by any app on the device, so the redirect must be assumed
readable by a hostile one. What it is allowed to contain is a result. What it must never contain
is the access token, the authorization code, the state, or the PKCE verifier -- all of which were
in scope moments earlier in the same request.
"""

import datetime
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from app.core import app_check, token_crypto
from app.core.config import settings
from app.models.provider_connection import ProviderConnection
from app.models.provider_oauth_state import ProviderOAuthState
from app.services.professional_presence.providers.base import ProviderCredential, ProviderIdentity
from app.services.professional_presence.providers.github import GitHubOAuthProvider

_ACCESS_TOKEN = "gho_NotARealTokenJustATestValue"
_REDIRECT_URI = "https://api.example.com/professional-presence/connections/github/callback"


@pytest.fixture(autouse=True)
def github_configured(monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "test-client-id")
    monkeypatch.setattr(settings, "github_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "github_oauth_redirect_uri", _REDIRECT_URI)
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def stub_github(monkeypatch):
    """GitHub's three network calls, replaced. Everything either side of them is the real code
    path -- the router, the state service, the connection service and the database."""
    monkeypatch.setattr(
        GitHubOAuthProvider,
        "exchange_code",
        lambda self, code, verifier: ProviderCredential(access_token=_ACCESS_TOKEN, granted_scopes=""),
    )
    monkeypatch.setattr(
        GitHubOAuthProvider,
        "fetch_identity",
        lambda self, token: ProviderIdentity(provider_user_id="583231", username="octocat"),
    )
    monkeypatch.setattr(GitHubOAuthProvider, "revoke", lambda self, token: None)


def _auth(client, email="presence@example.com"):
    client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _start(client, headers):
    """Begin a flow and return the raw state from the authorization URL."""
    response = client.post("/professional-presence/connections/github/start", headers=headers)
    assert response.status_code == 200, response.text
    url = response.json()["authorization_url"]
    return parse_qs(urlparse(url).query)["state"][0]


def _callback(client, state, code="the-code"):
    # follow_redirects=False because the redirect target is a custom scheme the test client
    # cannot follow -- and because the Location header is exactly what is under test.
    return client.get(
        "/professional-presence/connections/github/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def _result(response):
    return parse_qs(urlparse(response.headers["location"]).query)


class TestStart:
    def test_it_requires_authentication(self, client):
        assert client.post("/professional-presence/connections/github/start").status_code == 401

    def test_it_returns_a_pkce_authorization_url(self, client):
        headers = _auth(client)
        body = client.post(
            "/professional-presence/connections/github/start", headers=headers
        ).json()
        params = parse_qs(urlparse(body["authorization_url"]).query)

        assert params["code_challenge_method"] == ["S256"]
        assert params["code_challenge"]
        assert "scope" not in params
        assert body["expires_in_seconds"] > 0

    def test_the_response_never_contains_the_client_secret_or_a_verifier(self, client):
        headers = _auth(client)
        raw = client.post(
            "/professional-presence/connections/github/start", headers=headers
        ).text
        assert "test-client-secret" not in raw
        # The verifier stays server-side; only its sha256 challenge is ever transmitted.
        state = _start(client, headers)
        assert "code_verifier" not in raw
        assert state

    def test_it_is_unavailable_when_the_provider_is_not_configured(self, client, monkeypatch):
        monkeypatch.setattr(settings, "github_client_id", None)
        headers = _auth(client)
        response = client.post("/professional-presence/connections/github/start", headers=headers)
        assert response.status_code == 503

    def test_an_unknown_provider_is_a_404(self, client):
        headers = _auth(client)
        response = client.post("/professional-presence/connections/nitter/start", headers=headers)
        assert response.status_code == 404


class TestCallbackHappyPath:
    def test_a_valid_callback_connects_the_student(self, client):
        headers = _auth(client)
        state = _start(client, headers)

        response = _callback(client, state)

        assert response.status_code == 302
        assert _result(response)["status"] == ["connected"]

        connections = client.get("/professional-presence/connections", headers=headers).json()
        github = next(c for c in connections if c["provider"] == "github")
        assert github["connected"] is True
        assert github["username"] == "octocat"

    def test_the_redirect_back_into_the_app_carries_no_secret(self, client):
        """The assertion this whole design exists to satisfy.

        Any app on the device can register a custom scheme, so this URL must be assumed
        readable by a hostile one. It may carry a result; it must never carry the token, the
        code, the state, or the verifier."""
        headers = _auth(client)
        state = _start(client, headers)

        location = _callback(client, state).headers["location"]

        assert _ACCESS_TOKEN not in location
        assert state not in location
        assert "the-code" not in location
        assert "test-client-secret" not in location
        for forbidden in ("access_token", "token", "code", "code_verifier", "state="):
            assert forbidden not in location, f"{forbidden} leaked into the app redirect"

    def test_the_access_token_is_stored_encrypted(self, client):
        from app.api.deps import get_db
        from app.main import app

        headers = _auth(client)
        _callback(client, _start(client, headers))

        db = next(app.dependency_overrides[get_db]())
        row = db.query(ProviderConnection).one()
        assert row.access_token_encrypted != _ACCESS_TOKEN
        assert token_crypto.decrypt(row.access_token_encrypted) == _ACCESS_TOKEN
        assert row.provider_user_id == "583231"

    def test_the_status_response_never_contains_a_token(self, client):
        headers = _auth(client)
        _callback(client, _start(client, headers))

        raw = client.get("/professional-presence/connections", headers=headers).text
        assert _ACCESS_TOKEN not in raw
        assert "access_token" not in raw

    def test_unconnected_providers_are_still_reported(self, client):
        """The browser flow can end with no callback at all, so the app has to be able to ask
        the server what happened. "Not connected" must be a stated answer, not an omission."""
        headers = _auth(client)
        connections = client.get("/professional-presence/connections", headers=headers).json()
        assert {c["provider"] for c in connections} == {"github", "linkedin"}
        assert all(c["connected"] is False for c in connections)


class TestCallbackStateSecurity:
    def test_a_replayed_callback_is_refused(self, client):
        headers = _auth(client)
        state = _start(client, headers)
        assert _result(_callback(client, state))["status"] == ["connected"]

        replay = _callback(client, state)

        assert _result(replay)["status"] == ["failed"]
        assert _result(replay)["reason"] == ["invalid_request"]

    def test_a_replay_does_not_create_a_second_connection(self, client):
        from app.api.deps import get_db
        from app.main import app

        headers = _auth(client)
        state = _start(client, headers)
        _callback(client, state)
        _callback(client, state)

        db = next(app.dependency_overrides[get_db]())
        assert db.query(ProviderConnection).filter(ProviderConnection.revoked_at.is_(None)).count() == 1

    def test_an_unknown_state_is_refused(self, client):
        response = _callback(client, "a-state-that-was-never-issued")
        assert _result(response)["reason"] == ["invalid_request"]

    def test_an_expired_state_is_refused(self, client):
        from app.api.deps import get_db
        from app.main import app

        headers = _auth(client)
        state = _start(client, headers)

        db = next(app.dependency_overrides[get_db]())
        row = db.query(ProviderOAuthState).one()
        row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        db.commit()

        assert _result(_callback(client, state))["reason"] == ["invalid_request"]

    def test_every_state_failure_looks_identical_to_the_caller(self, client):
        """No oracle. If "expired" were distinguishable from "never existed", the endpoint would
        confirm which states had once been real -- to anybody who asked."""
        unknown = _result(_callback(client, "never-issued"))
        headers = _auth(client)
        state = _start(client, headers)
        _callback(client, state)
        replayed = _result(_callback(client, state))

        assert unknown["reason"] == replayed["reason"] == ["invalid_request"]

    def test_a_callback_without_a_state_is_refused(self, client):
        response = client.get(
            "/professional-presence/connections/github/callback",
            params={"code": "the-code"},
            follow_redirects=False,
        )
        assert _result(response)["reason"] == ["invalid_request"]

    def test_a_callback_without_a_code_is_refused(self, client):
        headers = _auth(client)
        state = _start(client, headers)
        response = client.get(
            "/professional-presence/connections/github/callback",
            params={"state": state},
            follow_redirects=False,
        )
        assert _result(response)["reason"] == ["invalid_request"]

    def test_a_declined_consent_screen_is_reported_as_cancelled(self, client):
        """The student pressed Cancel on GitHub. Not an error of ours and not worth alarming
        them about."""
        response = client.get(
            "/professional-presence/connections/github/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )
        assert _result(response)["status"] == ["cancelled"]

    def test_a_state_issued_for_another_provider_is_refused(self, client):
        """State swapping. The state row records which provider it was minted for, so it cannot
        be spent at a different provider's callback."""
        from app.api.deps import get_db
        from app.main import app

        headers = _auth(client)
        state = _start(client, headers)

        db = next(app.dependency_overrides[get_db]())
        row = db.query(ProviderOAuthState).one()
        row.provider = "linkedin"
        db.commit()

        assert _result(_callback(client, state))["reason"] == ["invalid_request"]


class TestIdentityBinding:
    def test_the_connection_binds_to_the_student_who_started_the_flow(self, client):
        """Cross-user binding. The callback carries no identity of its own, and `complete()`
        takes no profile argument -- so the only student it can bind is the one recorded on the
        state row when the flow began."""
        from app.api.deps import get_db
        from app.main import app

        first = _auth(client, "bind-a@example.com")
        state = _start(client, first)
        # A second student is signed in and active meanwhile; the callback must ignore them
        # entirely, because it never looks at who is calling it.
        _auth(client, "bind-b@example.com")

        _callback(client, state)

        db = next(app.dependency_overrides[get_db]())
        connection = db.query(ProviderConnection).one()
        owner = client.get("/professional-presence/connections", headers=first).json()
        assert next(c for c in owner if c["provider"] == "github")["connected"] is True
        assert connection.profile_id is not None

    def test_two_students_cannot_hold_the_same_github_account(self, client):
        """One GitHub identity, one live connection. Otherwise one student's public repositories
        would count as another student's career evidence."""
        first = _auth(client, "dup-a@example.com")
        _callback(client, _start(client, first))

        second = _auth(client, "dup-b@example.com")
        response = _callback(client, _start(client, second))

        assert _result(response)["reason"] == ["already_connected"]
        second_status = client.get("/professional-presence/connections", headers=second).json()
        assert next(c for c in second_status if c["provider"] == "github")["connected"] is False

    def test_a_rejected_duplicate_leaves_the_first_student_connected(self, client):
        """A failed attempt by somebody else must not disturb the student who legitimately holds
        the account."""
        first = _auth(client, "keep-a@example.com")
        _callback(client, _start(client, first))

        second = _auth(client, "keep-b@example.com")
        _callback(client, _start(client, second))

        first_status = client.get("/professional-presence/connections", headers=first).json()
        assert next(c for c in first_status if c["provider"] == "github")["connected"] is True

    def test_reconnecting_replaces_rather_than_duplicates(self, client):
        from app.api.deps import get_db
        from app.main import app

        headers = _auth(client, "reconnect@example.com")
        _callback(client, _start(client, headers))
        _callback(client, _start(client, headers))

        db = next(app.dependency_overrides[get_db]())
        live = db.query(ProviderConnection).filter(ProviderConnection.revoked_at.is_(None)).all()
        assert len(live) == 1
        assert db.query(ProviderConnection).count() == 2


class TestProviderFailures:
    @pytest.mark.parametrize(
        "error_cls,expected_reason",
        [
            ("ProviderUnauthorized", "unauthorized"),
            ("ProviderRateLimited", "rate_limit"),
            ("ProviderTimeout", "timeout"),
            ("ProviderUnavailable", "unavailable"),
            ("ProviderMalformedResponse", "malformed"),
            ("ProviderForbidden", "forbidden"),
            ("ProviderNotFound", "not_found"),
        ],
    )
    def test_each_failure_reaches_the_app_as_a_category(self, client, monkeypatch, error_cls, expected_reason):
        from app.services.professional_presence import errors

        def _raise(self, code, verifier):
            raise getattr(errors, error_cls)()

        monkeypatch.setattr(GitHubOAuthProvider, "exchange_code", _raise)

        headers = _auth(client)
        response = _callback(client, _start(client, headers))

        assert _result(response)["reason"] == [expected_reason]

    def test_a_provider_failure_leaves_no_half_built_connection(self, client, monkeypatch):
        from app.api.deps import get_db
        from app.main import app
        from app.services.professional_presence import errors

        def _raise(self, token):
            raise errors.ProviderUnavailable()

        monkeypatch.setattr(GitHubOAuthProvider, "fetch_identity", _raise)

        headers = _auth(client)
        _callback(client, _start(client, headers))

        db = next(app.dependency_overrides[get_db]())
        assert db.query(ProviderConnection).count() == 0


class TestDisconnect:
    def test_it_destroys_the_credential_and_keeps_the_audit_row(self, client):
        from app.api.deps import get_db
        from app.main import app

        headers = _auth(client)
        _callback(client, _start(client, headers))

        assert client.delete("/professional-presence/connections/github", headers=headers).status_code == 204

        db = next(app.dependency_overrides[get_db]())
        row = db.query(ProviderConnection).one()
        assert row.access_token_encrypted is None
        assert row.revoked_at is not None
        assert row.status == "revoked"

    def test_disconnecting_what_was_never_connected_is_a_404(self, client):
        headers = _auth(client)
        assert client.delete("/professional-presence/connections/github", headers=headers).status_code == 404

    def test_it_requires_authentication(self, client):
        assert client.delete("/professional-presence/connections/github").status_code == 401

    def test_it_only_touches_the_callers_own_connection(self, client):
        """Scoped by the bearer token, so it cannot be aimed at somebody else's connection."""
        first = _auth(client, "own-a@example.com")
        _callback(client, _start(client, first))

        second = _auth(client, "own-b@example.com")
        assert client.delete("/professional-presence/connections/github", headers=second).status_code == 404

        first_status = client.get("/professional-presence/connections", headers=first).json()
        assert next(c for c in first_status if c["provider"] == "github")["connected"] is True

    def test_a_provider_outage_does_not_block_disconnection(self, client, monkeypatch):
        """The student must end up disconnected even when GitHub cannot be reached -- otherwise
        an outage would trap them in a connection they have asked to end."""
        from app.services.professional_presence import errors

        def _raise(self, token):
            raise errors.ProviderUnavailable()

        headers = _auth(client)
        _callback(client, _start(client, headers))
        monkeypatch.setattr(GitHubOAuthProvider, "revoke", _raise)

        assert client.delete("/professional-presence/connections/github", headers=headers).status_code == 204

    def test_reconnecting_after_disconnecting_works(self, client):
        """The partial unique indexes exist so that disconnecting genuinely releases the slot."""
        headers = _auth(client)
        _callback(client, _start(client, headers))
        client.delete("/professional-presence/connections/github", headers=headers)

        assert _result(_callback(client, _start(client, headers)))["status"] == ["connected"]


class TestAppCheck:
    def test_the_callback_is_reachable_without_an_app_check_header(self, client, monkeypatch):
        """The precedent this feature depends on. GitHub redirects the student's BROWSER here,
        and a browser carries no X-Firebase-AppCheck header -- so router-level App Check would
        401 every successful authorisation. Same treatment POST /subscriptions/webhook already
        gets for Razorpay."""
        monkeypatch.setattr(settings, "firebase_app_check_enabled", True)
        monkeypatch.setattr(app_check, "_get_app_check_module", lambda: None)

        response = client.get(
            "/professional-presence/connections/github/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_every_other_route_still_requires_app_check(self, client, monkeypatch):
        """The exemption must be exactly one route wide. If enabling App Check stopped guarding
        the authenticated endpoints, the callback exemption would have quietly become a
        router-wide hole."""
        headers = _auth(client)
        monkeypatch.setattr(settings, "firebase_app_check_enabled", True)

        assert client.get("/professional-presence/connections", headers=headers).status_code == 401
        assert client.post(
            "/professional-presence/connections/github/start", headers=headers
        ).status_code == 401
        assert client.delete(
            "/professional-presence/connections/github", headers=headers
        ).status_code == 401


class TestLogging:
    def test_no_credential_reaches_the_logs(self, client, caplog):
        """An info line carrying a token is a credential in a log aggregator, where it is
        indexed and retained. The authorization URL counts too, because it carries the state as
        a query parameter.

        Scoped to loggers the APPLICATION owns. `httpx` is excluded deliberately and not because
        it is inconvenient: inside a TestClient, httpx is the thing *making* the request and
        logs the full URL it dialled, query string included. In production httpx is never on
        that side -- the application is the server, and the inbound URL reaches our logs only
        through the middleware asserted in the next test.
        """
        import logging

        caplog.set_level(logging.DEBUG)
        headers = _auth(client)
        state = _start(client, headers)
        _callback(client, state)
        _callback(client, state)  # a replay, to exercise the warning path as well

        logged = "\n".join(
            record.getMessage() for record in caplog.records if not record.name.startswith("httpx")
        )
        assert _ACCESS_TOKEN not in logged
        assert state not in logged
        assert "test-client-secret" not in logged
        assert "the-code" not in logged
        assert "https://github.com/login/oauth/authorize" not in logged

    def test_the_request_log_records_the_path_without_its_query_string(self, client, caplog):
        """The property the test above depends on, pinned separately so it cannot quietly stop
        being true.

        The state and the authorization code both arrive as query parameters on the callback --
        there is nowhere else for OAuth to put them -- so a request log that recorded query
        strings would write a live credential to disk on every connection. app/core/
        observability.py logs `scope["path"]`, which excludes them. A future change to log the
        full URL for debugging would be caught here rather than in an incident.
        """
        import logging

        caplog.set_level(logging.INFO)
        headers = _auth(client)
        state = _start(client, headers)
        _callback(client, state)

        observability = [
            record.getMessage()
            for record in caplog.records
            if record.name == "app.core.observability"
        ]
        callback_lines = [line for line in observability if "callback" in line]
        assert callback_lines, "the callback request was not logged at all"
        for line in callback_lines:
            assert state not in line
            assert "the-code" not in line
            assert "?" not in line
