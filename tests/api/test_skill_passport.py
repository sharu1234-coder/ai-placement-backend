import pytest


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "passport@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "passport@example.com", "password": "supersecret123"}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "passport2@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "passport2@example.com", "password": "supersecret123"}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def _sde_role_id(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    return next(r["id"] for r in roles if r["name"] == "SDE")


def test_requires_auth(client):
    assert client.get("/skill-passport").status_code == 401


def test_no_role_returns_empty_list_not_fabricated_skills(client, auth_headers):
    response = client.get("/skill-passport", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_list_and_detail_agree_after_target_role_selection(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    listed = client.get("/skill-passport", headers=auth_headers).json()
    assert len(listed) > 0
    assert all(item["state"] == "claimed" for item in listed)
    assert all(item["level"] is None for item in listed)

    skill = listed[0]["skill"]
    detail = client.get(f"/skill-passport/{skill}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json() == listed[0]


def test_unknown_skill_returns_404(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    response = client.get("/skill-passport/TotallyMadeUpSkillXYZ", headers=auth_headers)
    assert response.status_code == 404


def test_skill_passport_is_isolated_per_profile(client, auth_headers, other_auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    mine = client.get("/skill-passport", headers=auth_headers).json()
    theirs = client.get("/skill-passport", headers=other_auth_headers).json()
    assert len(mine) > 0
    assert theirs == []
