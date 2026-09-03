import pytest


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "twin@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "twin@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "twin2@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "twin2@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sde_role_id(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    return next(r["id"] for r in roles if r["name"] == "SDE")


def test_requires_auth(client):
    response = client.get("/career-twin")
    assert response.status_code == 403  # no bearer token supplied at all


def test_no_role_returns_has_role_false_not_a_fabricated_zero(client, auth_headers):
    response = client.get("/career-twin", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["has_role"] is False
    assert body["readiness_percent"] is None
    assert body["skills"] == []


def test_target_role_selection_powers_career_twin(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    response = client.get("/career-twin", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["has_role"] is True
    assert body["role_source"] == "target_role"
    assert body["display_role_name"] == "SDE"
    assert len(body["skills"]) > 0
    assert all(s["state"] == "claimed" for s in body["skills"])  # no evidence yet, honestly reported


def test_skills_endpoint_matches_full_view(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    full = client.get("/career-twin", headers=auth_headers).json()
    skills_only = client.get("/career-twin/skills", headers=auth_headers).json()
    assert skills_only == full["skills"]


def test_single_skill_lookup_and_404_for_unknown_skill(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)
    known_skill = client.get("/career-twin/skills", headers=auth_headers).json()[0]["skill"]

    found = client.get(f"/career-twin/skills/{known_skill}", headers=auth_headers)
    assert found.status_code == 200
    assert found.json()["skill"] == known_skill

    missing = client.get("/career-twin/skills/TotallyMadeUpSkillXYZ", headers=auth_headers)
    assert missing.status_code == 404


def test_readiness_endpoint_omits_skill_list(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    response = client.get("/career-twin/readiness", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert "skills" not in body
    assert body["has_role"] is True


def test_career_twin_is_isolated_per_profile(client, auth_headers, other_auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    mine = client.get("/career-twin", headers=auth_headers).json()
    theirs = client.get("/career-twin", headers=other_auth_headers).json()
    assert mine["has_role"] is True
    assert theirs["has_role"] is False
