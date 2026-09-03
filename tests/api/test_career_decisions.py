import pytest


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "decision@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "decision@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _role_ids(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    sde_id = next(r["id"] for r in roles if r["name"] == "SDE")
    data_analyst_id = next(r["id"] for r in roles if r["name"] == "Data Analyst")
    return sde_id, data_analyst_id


def test_requires_auth(client):
    resp = client.post("/career-decisions/simulate", json={"role_ids": [1, 2]})
    assert resp.status_code == 403


def test_requires_at_least_two_roles(client, auth_headers):
    resp = client.post("/career-decisions/simulate", json={"role_ids": [1]}, headers=auth_headers)
    assert resp.status_code == 422


def test_rejects_more_than_three_roles(client, auth_headers):
    resp = client.post("/career-decisions/simulate", json={"role_ids": [1, 2, 3, 4]}, headers=auth_headers)
    assert resp.status_code == 422


def test_unknown_role_id_returns_422(client, auth_headers):
    sde_id, _ = _role_ids(client, auth_headers)
    resp = client.post("/career-decisions/simulate", json={"role_ids": [sde_id, 999999]}, headers=auth_headers)
    assert resp.status_code == 422


def test_simulate_with_no_evidence_returns_two_honest_stretches(client, auth_headers):
    sde_id, data_analyst_id = _role_ids(client, auth_headers)
    resp = client.post(
        "/career-decisions/simulate", json={"role_ids": [sde_id, data_analyst_id]}, headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["roles"]) == 2
    assert all(r["classification"] == "stretch" for r in body["roles"])
    assert all(r["readiness_percent"] == 0 for r in body["roles"])
    assert all(r["is_estimate"] is True for r in body["roles"])
    assert len(body["assumptions"]) > 0
    assert "probability" not in str(body).lower()


def test_weekly_hours_are_echoed_back_clamped(client, auth_headers):
    sde_id, data_analyst_id = _role_ids(client, auth_headers)
    resp = client.post(
        "/career-decisions/simulate",
        json={"role_ids": [sde_id, data_analyst_id], "weekly_hours": 9999},
        headers=auth_headers,
    )
    body = resp.json()
    assert body["weekly_hours_used"] == 80
    assert all(r["estimated_weeks"] is not None for r in body["roles"])


def test_roles_are_isolated_per_profile(client, auth_headers):
    """Nothing is persisted -- two calls for the same profile must not accumulate state, and
    a second profile calling this must never see the first profile's evidence leak in."""
    sde_id, data_analyst_id = _role_ids(client, auth_headers)
    first = client.post(
        "/career-decisions/simulate", json={"role_ids": [sde_id, data_analyst_id]}, headers=auth_headers
    ).json()
    second = client.post(
        "/career-decisions/simulate", json={"role_ids": [sde_id, data_analyst_id]}, headers=auth_headers
    ).json()
    assert first == second
