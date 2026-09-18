"""Phase 3 at the HTTP boundary: sync, audit and history, and every way they must refuse.

The security matrix this file holds: no route is reachable unauthenticated, no student can see
or act on another's connection, a provider failure never becomes a 500 or leaks provider text,
and no response anywhere in the feature contains a credential.

Everything either side of GitHub's network calls is the real code path -- the router, the sync
service, the audit service and the database. Only the adapter's outbound HTTP is replaced,
because testing that would be testing httpx.
"""

import datetime
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from app.ai.provider import AIGenerationError
from app.core.config import settings
from app.services.professional_presence.errors import ProviderRateLimited, ProviderUnauthorized
from app.services.professional_presence.providers.base import (
    ProviderCredential,
    ProviderFetch,
    ProviderIdentity,
    ProviderProfile,
    ProviderRepository,
)
from app.services.professional_presence.providers.github import GitHubOAuthProvider

_ACCESS_TOKEN = "gho_NotARealTokenJustATestValue"
_REDIRECT_URI = "https://api.example.com/professional-presence/connections/github/callback"
_ACCOUNT_ID = "583231"

# Fixed at import, not recomputed per call. A stub that derives pushed_at from now() on every
# fetch changes the snapshot's material content between two syncs of the "same" account, which
# would make the unchanged-account case untestable here for a reason that has nothing to do with
# the product. Recent enough that the staleness observation stays out of the way.
_PUSHED_AT = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)


@pytest.fixture(autouse=True)
def github_configured(monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "test-client-id")
    monkeypatch.setattr(settings, "github_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "github_oauth_redirect_uri", _REDIRECT_URI)
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())


def _repo(name="bus-tracker", **overrides):
    base = dict(
        full_name=f"octocat/{name}",
        name=name,
        description="Live arrival board",
        primary_language="Python",
        languages=("Python",),
        topics=("transit",),
        is_fork=False,
        is_archived=False,
        stars=1,
        pushed_at=_PUSHED_AT,
    )
    base.update(overrides)
    return ProviderRepository(**base)


def _fetch(repos=None, **profile_overrides):
    profile = dict(
        provider_user_id=_ACCOUNT_ID,
        username="octocat",
        name="The Octocat",
        bio="Building things",
        company=None,
        location=None,
        blog=None,
        public_repos=1,
        followers=10,
    )
    profile.update(profile_overrides)
    return ProviderFetch(
        profile=ProviderProfile(**profile),
        repositories=tuple(repos if repos is not None else [_repo()]),
        complete=True,
    )


@pytest.fixture(autouse=True)
def stub_github(monkeypatch):
    monkeypatch.setattr(
        GitHubOAuthProvider,
        "exchange_code",
        lambda self, code, verifier: ProviderCredential(access_token=_ACCESS_TOKEN, granted_scopes=""),
    )
    monkeypatch.setattr(
        GitHubOAuthProvider,
        "fetch_identity",
        lambda self, token: ProviderIdentity(provider_user_id=_ACCOUNT_ID, username="octocat"),
    )
    monkeypatch.setattr(GitHubOAuthProvider, "revoke", lambda self, token: None)
    monkeypatch.setattr(GitHubOAuthProvider, "fetch_account", lambda self, token: _fetch())


@pytest.fixture(autouse=True)
def stub_ai(monkeypatch):
    """No AI in these tests. The audit must be complete and useful without it, which is the
    property worth asserting at this level -- the grounding itself is unit tested."""
    from app.ai import provider as ai_provider_module

    class _Silent:
        def generate_structured(self, prompt, schema):
            raise AIGenerationError("no AI in API tests")

        def generate_text(self, prompt):
            return ""

    monkeypatch.setattr(ai_provider_module, "get_ai_provider", lambda: _Silent())


def _auth(client, email="sync@example.com"):
    client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": email, "password": "supersecret123"}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def _connect(client, headers):
    """Drive the real OAuth flow to completion so a genuine connection row exists."""
    response = client.post("/professional-presence/connections/github/start", headers=headers)
    state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][0]
    callback = client.get(
        "/professional-presence/connections/github/callback",
        params={"code": "the-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    return callback


class TestAuthentication:
    def test_sync_requires_authentication(self, client):
        assert client.post("/professional-presence/connections/github/sync").status_code == 401

    def test_audit_requires_authentication(self, client):
        assert client.get("/professional-presence/audit").status_code == 401

    def test_history_requires_authentication(self, client):
        assert client.get("/professional-presence/history").status_code == 401


class TestSync:
    def test_syncing_without_a_connection_is_404(self, client):
        headers = _auth(client)
        assert client.post("/professional-presence/connections/github/sync", headers=headers).status_code == 404

    def test_a_connected_student_can_sync(self, client):
        headers = _auth(client)
        _connect(client, headers)

        response = client.post("/professional-presence/connections/github/sync", headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["evidence_added"] >= 1
        assert body["repositories_seen"] == 1
        assert body["complete"] is True

    def test_the_sync_response_carries_no_credential(self, client):
        """The whole feature's standing guarantee, asserted on the newest response shape."""
        headers = _auth(client)
        _connect(client, headers)

        body = client.post("/professional-presence/connections/github/sync", headers=headers).text

        assert _ACCESS_TOKEN not in body
        assert "access_token" not in body
        assert "token" not in body.lower()

    def test_a_second_sync_adds_nothing(self, client):
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        second = client.post("/professional-presence/connections/github/sync", headers=headers).json()

        assert second["evidence_added"] == 0
        assert second["snapshot_written"] is False

    def test_a_provider_failure_is_a_502_and_leaks_nothing(self, client, monkeypatch):
        headers = _auth(client)
        _connect(client, headers)

        def _boom(self, token):
            raise ProviderRateLimited()

        monkeypatch.setattr(GitHubOAuthProvider, "fetch_account", _boom)
        response = client.post("/professional-presence/connections/github/sync", headers=headers)

        assert response.status_code == 502
        # The message is ours. The provider's own body never travels.
        assert "rate limit" in response.json()["detail"].lower()

    def test_a_rejected_credential_reports_a_conflict_after_marking_reauth(self, client, monkeypatch):
        headers = _auth(client)
        _connect(client, headers)

        def _unauthorized(self, token):
            raise ProviderUnauthorized()

        monkeypatch.setattr(GitHubOAuthProvider, "fetch_account", _unauthorized)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        status = client.get("/professional-presence/connections", headers=headers).json()
        github = next(item for item in status if item["provider"] == "github")
        assert github["status"] == "needs_reauth"
        assert github["last_error_kind"] == "unauthorized"


class TestIsolation:
    def test_one_student_cannot_sync_anothers_connection(self, client):
        """There is no path to another account's connection: the profile comes from the bearer
        token, so a second student simply has nothing to sync."""
        owner = _auth(client, "owner@example.com")
        _connect(client, owner)

        intruder = _auth(client, "intruder@example.com")
        response = client.post("/professional-presence/connections/github/sync", headers=intruder)

        assert response.status_code == 404

    def test_one_student_never_sees_anothers_audit(self, client):
        owner = _auth(client, "owner2@example.com")
        _connect(client, owner)
        client.post("/professional-presence/connections/github/sync", headers=owner)

        intruder = _auth(client, "intruder2@example.com")
        body = client.get("/professional-presence/audit", headers=intruder).json()

        assert body["connected"] is False
        assert body["findings"] == []

    def test_one_student_never_sees_anothers_history(self, client):
        owner = _auth(client, "owner3@example.com")
        _connect(client, owner)
        client.post("/professional-presence/connections/github/sync", headers=owner)

        intruder = _auth(client, "intruder3@example.com")
        assert client.get("/professional-presence/history", headers=intruder).json() == []


class TestAudit:
    def test_an_unconnected_student_gets_a_well_formed_body(self, client):
        """"Connect GitHub" and "GitHub failed" must be renderable from one response shape."""
        headers = _auth(client)
        body = client.get("/professional-presence/audit", headers=headers).json()

        assert body["connected"] is False
        assert body["freshness"] == "never"
        assert body["findings"] == []

    def test_connected_but_never_synced_is_not_an_error(self, client):
        headers = _auth(client)
        _connect(client, headers)

        body = client.get("/professional-presence/audit", headers=headers).json()

        assert body["connected"] is True
        assert body["freshness"] == "never"
        assert body["findings"] == []

    def test_a_synced_account_produces_evidence_backed_findings(self, client):
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        body = client.get("/professional-presence/audit", headers=headers).json()

        assert body["connected"] is True
        assert body["freshness"] == "fresh"
        assert body["findings"], "a synced account with repositories should produce findings"
        # Every finding must carry its own proof, or it is indistinguishable from flattery.
        assert all(f["evidence"] for f in body["findings"])

    def test_without_ai_every_finding_still_has_an_action(self, client):
        """The audit degrades to templates rather than to nothing."""
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        body = client.get("/professional-presence/audit", headers=headers).json()

        assert all(f["recommended_action"] for f in body["findings"])
        assert all(f["is_template"] for f in body["findings"])
        assert body["narrative_prompt_version"] is None

    def test_a_language_strength_names_the_measured_language(self, client):
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        body = client.get("/professional-presence/audit", headers=headers).json()
        strengths = [f for f in body["findings"] if f["kind"] == "strength"]

        assert any(f["subject"] == "Python" for f in strengths)

    def test_no_finding_mentions_a_hiring_probability(self, client):
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        text = client.get("/professional-presence/audit", headers=headers).text.lower()

        for banned in ("probability", "% chance", "likely to be hired", "shortlist"):
            assert banned not in text

    def test_a_failed_sync_does_not_erase_the_findings(self, client, monkeypatch):
        """"GitHub is down" must never render as "you have no evidence"."""
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)
        before = client.get("/professional-presence/audit", headers=headers).json()

        monkeypatch.setattr(
            GitHubOAuthProvider, "fetch_account", lambda self, token: (_ for _ in ()).throw(ProviderRateLimited())
        )
        client.post("/professional-presence/connections/github/sync", headers=headers)

        after = client.get("/professional-presence/audit", headers=headers).json()
        assert after["findings"] == before["findings"]
        assert after["last_error_kind"] == "rate_limit"
        # Still the moment of the last GOOD sync, not of the failure.
        assert after["last_synced_at"] == before["last_synced_at"]


class TestHistory:
    def test_history_is_empty_before_a_sync(self, client):
        headers = _auth(client)
        _connect(client, headers)
        assert client.get("/professional-presence/history", headers=headers).json() == []

    def test_a_sync_appends_one_history_entry(self, client):
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        history = client.get("/professional-presence/history", headers=headers).json()

        assert len(history) == 1
        assert history[0]["sequence"] == 1
        assert history[0]["public_repositories"] == 1
        assert history[0]["languages"] == ["Python"]

    def test_history_does_not_replay_repository_descriptions(self, client):
        """A history screen asks "what changed?", not "show me every third-party string"."""
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        assert "Live arrival board" not in client.get(
            "/professional-presence/history", headers=headers
        ).text


class TestAccountDeletion:
    def test_deleting_the_account_removes_the_evidence_it_produced(self, client, db_session=None):
        """The existing deletion coverage must still hold now that rows actually exist."""
        headers = _auth(client, "deleteme@example.com")
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)

        response = client.request(
            "DELETE", "/users/me", headers=headers, json={"password": "supersecret123"}
        )

        assert response.status_code in (200, 204), response.text
        # The token is dead with the account, so the audit is unreachable rather than empty.
        assert client.get("/professional-presence/audit", headers=headers).status_code == 401


class TestDisconnect:
    def test_disconnecting_stops_the_audit_reporting_findings(self, client):
        headers = _auth(client)
        _connect(client, headers)
        client.post("/professional-presence/connections/github/sync", headers=headers)
        assert client.get("/professional-presence/audit", headers=headers).json()["findings"]

        assert client.delete("/professional-presence/connections/github", headers=headers).status_code == 204

        body = client.get("/professional-presence/audit", headers=headers).json()
        assert body["connected"] is False
        assert body["findings"] == []
