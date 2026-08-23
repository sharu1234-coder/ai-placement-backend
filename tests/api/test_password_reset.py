import pytest

from app.core.config import settings
from app.email.provider import EmailProvider, get_email_provider
from app.main import app


class FakeEmailProvider(EmailProvider):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_password_reset_email(self, to_email: str, code: str) -> None:
        self.sent.append((to_email, code))


@pytest.fixture()
def fake_email():
    provider = FakeEmailProvider()
    app.dependency_overrides[get_email_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_email_provider, None)


def _signup(client, email: str, password: str = "supersecret123"):
    client.post("/auth/signup", json={"email": email, "password": password})


def test_forgot_password_unknown_email_returns_generic_message(client, fake_email):
    resp = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert "reset code has been sent" in resp.json()["message"]
    assert fake_email.sent == []


def test_forgot_password_known_email_sends_code(client, fake_email):
    _signup(client, "reset@example.com")

    resp = client.post("/auth/forgot-password", json={"email": "reset@example.com"})
    assert resp.status_code == 200
    assert "reset code has been sent" in resp.json()["message"]
    assert len(fake_email.sent) == 1
    assert fake_email.sent[0][0] == "reset@example.com"
    assert len(fake_email.sent[0][1]) == 8


def test_reset_password_with_valid_code_succeeds(client, fake_email):
    _signup(client, "flow@example.com", "old-password123")
    client.post("/auth/forgot-password", json={"email": "flow@example.com"})
    code = fake_email.sent[0][1]

    resp = client.post(
        "/auth/reset-password",
        json={"email": "flow@example.com", "code": code, "new_password": "new-password456"},
    )
    assert resp.status_code == 200

    assert client.post(
        "/auth/login", json={"email": "flow@example.com", "password": "old-password123"}
    ).status_code == 401
    assert client.post(
        "/auth/login", json={"email": "flow@example.com", "password": "new-password456"}
    ).status_code == 200


def test_reset_password_with_wrong_code_fails(client, fake_email):
    _signup(client, "wrongcode@example.com")
    client.post("/auth/forgot-password", json={"email": "wrongcode@example.com"})

    resp = client.post(
        "/auth/reset-password",
        json={"email": "wrongcode@example.com", "code": "deadbeef", "new_password": "new-password456"},
    )
    assert resp.status_code == 400


def test_reset_password_code_is_single_use(client, fake_email):
    _signup(client, "singleuse@example.com")
    client.post("/auth/forgot-password", json={"email": "singleuse@example.com"})
    code = fake_email.sent[0][1]

    first = client.post(
        "/auth/reset-password",
        json={"email": "singleuse@example.com", "code": code, "new_password": "new-password456"},
    )
    assert first.status_code == 200

    second = client.post(
        "/auth/reset-password",
        json={"email": "singleuse@example.com", "code": code, "new_password": "another-password789"},
    )
    assert second.status_code == 400


def test_reset_password_unknown_email_fails(client, fake_email):
    resp = client.post(
        "/auth/reset-password",
        json={"email": "nobody@example.com", "code": "deadbeef", "new_password": "new-password456"},
    )
    assert resp.status_code == 400


def test_forgot_password_invalidates_previous_code(client, fake_email):
    _signup(client, "double@example.com")

    client.post("/auth/forgot-password", json={"email": "double@example.com"})
    first_code = fake_email.sent[0][1]

    client.post("/auth/forgot-password", json={"email": "double@example.com"})
    second_code = fake_email.sent[1][1]

    stale = client.post(
        "/auth/reset-password",
        json={"email": "double@example.com", "code": first_code, "new_password": "new-password456"},
    )
    assert stale.status_code == 400

    fresh = client.post(
        "/auth/reset-password",
        json={"email": "double@example.com", "code": second_code, "new_password": "new-password456"},
    )
    assert fresh.status_code == 200


def test_reset_password_invalidates_existing_access_token(client, fake_email):
    _signup(client, "invalidate@example.com", "old-password123")
    login_resp = client.post(
        "/auth/login", json={"email": "invalidate@example.com", "password": "old-password123"}
    )
    headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}
    # Token works before the reset (404 -- not 401/403 -- proves it authenticated fine).
    assert client.get("/resumes/999999/analysis", headers=headers).status_code == 404

    client.post("/auth/forgot-password", json={"email": "invalidate@example.com"})
    code = fake_email.sent[0][1]
    reset_resp = client.post(
        "/auth/reset-password",
        json={"email": "invalidate@example.com", "code": code, "new_password": "new-password456"},
    )
    assert reset_resp.status_code == 200

    # Whoever had that old token (possibly not the legitimate owner -- this is exactly the
    # scenario password reset exists to recover from) can no longer use it.
    assert client.get("/resumes/999999/analysis", headers=headers).status_code == 401


def test_reset_password_rejects_expired_code(client, fake_email, monkeypatch):
    monkeypatch.setattr(settings, "password_reset_token_expire_minutes", -1)
    _signup(client, "expired@example.com")
    client.post("/auth/forgot-password", json={"email": "expired@example.com"})
    code = fake_email.sent[0][1]

    resp = client.post(
        "/auth/reset-password",
        json={"email": "expired@example.com", "code": code, "new_password": "new-password456"},
    )
    assert resp.status_code == 400
