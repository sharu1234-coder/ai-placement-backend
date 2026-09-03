import pytest


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "nba@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "nba@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "nba2@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "nba2@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sde_role_id(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    return next(r["id"] for r in roles if r["name"] == "SDE")


def test_requires_auth(client):
    assert client.get("/next-best-action").status_code == 403


def test_no_role_has_no_recommendation(client, auth_headers):
    response = client.get("/next-best-action", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["has_recommendation"] is False
    assert body["tier"] is None


def test_target_role_with_no_evidence_yields_critical_role_gap(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    response = client.get("/next-best-action", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["has_recommendation"] is True
    assert body["tier"] == "critical_role_gap"
    assert body["estimated_minutes"] is not None
    assert len(body["reasons"]) > 0


def test_isolated_per_profile(client, auth_headers, other_auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    mine = client.get("/next-best-action", headers=auth_headers).json()
    theirs = client.get("/next-best-action", headers=other_auth_headers).json()
    assert mine["has_recommendation"] is True
    assert theirs["has_recommendation"] is False
