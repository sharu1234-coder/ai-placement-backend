import pytest


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "sub@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "sub@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_get_status_returns_free_coming_soon(client, auth_headers):
    resp = client.get("/subscriptions/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "free"
    assert body["status"] == "coming_soon"


def test_get_status_requires_auth(client):
    resp = client.get("/subscriptions/status")
    assert resp.status_code == 403
