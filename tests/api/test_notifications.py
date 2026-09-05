import pytest


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "notify@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "notify@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "notify2@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "notify2@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sde_role_id(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    return next(r["id"] for r in roles if r["name"] == "SDE")


def _complete_a_mission(client, auth_headers) -> None:
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)
    mission_id = client.get("/missions/today", headers=auth_headers).json()["id"]
    client.post(f"/missions/{mission_id}/complete", json={}, headers=auth_headers)


def test_requires_auth(client):
    assert client.get("/notifications/pending").status_code == 401


def test_empty_when_nothing_has_happened(client, auth_headers):
    response = client.get("/notifications/pending", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_a_real_mission_completion_surfaces_as_a_pending_notification(client, auth_headers):
    _complete_a_mission(client, auth_headers)

    pending = client.get("/notifications/pending", headers=auth_headers).json()
    assert len(pending) == 1
    assert pending[0]["kind"] == "mission_completed"
    assert pending[0]["title"]
    assert pending[0]["body"]


def test_ack_removes_it_from_pending(client, auth_headers):
    _complete_a_mission(client, auth_headers)
    event_id = client.get("/notifications/pending", headers=auth_headers).json()[0]["id"]

    ack = client.post("/notifications/ack", json={"ids": [event_id]}, headers=auth_headers)
    assert ack.status_code == 204

    assert client.get("/notifications/pending", headers=auth_headers).json() == []


def test_ack_is_idempotent(client, auth_headers):
    _complete_a_mission(client, auth_headers)
    event_id = client.get("/notifications/pending", headers=auth_headers).json()[0]["id"]

    client.post("/notifications/ack", json={"ids": [event_id]}, headers=auth_headers)
    second = client.post("/notifications/ack", json={"ids": [event_id]}, headers=auth_headers)
    assert second.status_code == 204


def test_cannot_ack_or_see_another_profiles_notifications(client, auth_headers, other_auth_headers):
    _complete_a_mission(client, auth_headers)
    event_id = client.get("/notifications/pending", headers=auth_headers).json()[0]["id"]

    # A different profile acking this id must be a silent no-op, never leaking that it exists.
    other_ack = client.post("/notifications/ack", json={"ids": [event_id]}, headers=other_auth_headers)
    assert other_ack.status_code == 204

    assert client.get("/notifications/pending", headers=other_auth_headers).json() == []
    # Unaffected -- the other profile's ack did not touch it.
    assert len(client.get("/notifications/pending", headers=auth_headers).json()) == 1
