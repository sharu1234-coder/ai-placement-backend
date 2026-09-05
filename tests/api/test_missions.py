import pytest


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "mission@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "mission@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "mission2@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "mission2@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sde_role_id(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    return next(r["id"] for r in roles if r["name"] == "SDE")


def test_requires_auth(client):
    assert client.get("/missions/today").status_code == 401


def test_get_today_creates_a_mission(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    response = client.get("/missions/today", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_started"
    assert body["tier"] == "critical_role_gap"


def test_get_today_is_idempotent(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    first = client.get("/missions/today", headers=auth_headers).json()
    second = client.get("/missions/today", headers=auth_headers).json()
    assert first["id"] == second["id"]


def test_start_and_complete_flow(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)
    mission_id = client.get("/missions/today", headers=auth_headers).json()["id"]

    started = client.post(f"/missions/{mission_id}/start", headers=auth_headers)
    assert started.status_code == 200
    assert started.json()["status"] == "in_progress"

    completed = client.post(f"/missions/{mission_id}/complete", json={}, headers=auth_headers)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["completed_at"] is not None


def test_duplicate_complete_does_not_error(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)
    mission_id = client.get("/missions/today", headers=auth_headers).json()["id"]

    first = client.post(f"/missions/{mission_id}/complete", json={}, headers=auth_headers).json()
    second = client.post(f"/missions/{mission_id}/complete", json={}, headers=auth_headers).json()
    assert second["completed_at"] == first["completed_at"]


def test_cannot_start_or_complete_another_profiles_mission(client, auth_headers, other_auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)
    mission_id = client.get("/missions/today", headers=auth_headers).json()["id"]

    assert client.post(f"/missions/{mission_id}/start", headers=other_auth_headers).status_code == 404
    assert client.post(f"/missions/{mission_id}/complete", json={}, headers=other_auth_headers).status_code == 404


def test_complete_with_unknown_assessment_id_returns_404(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)
    mission_id = client.get("/missions/today", headers=auth_headers).json()["id"]

    response = client.post(
        f"/missions/{mission_id}/complete", json={"assessment_id": 999999}, headers=auth_headers
    )
    assert response.status_code == 404
