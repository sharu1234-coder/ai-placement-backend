import pytest


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "profile@example.com", "password": "supersecret123"})
    login_resp = client.post(
        "/auth/login", json={"email": "profile@example.com", "password": "supersecret123"}
    )
    token = login_resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_get_my_profile_auto_creates(client, auth_headers):
    resp = client.get("/users/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_name"] is None
    assert body["target_role_id"] is None


def test_get_my_profile_requires_auth(client):
    resp = client.get("/users/me")
    assert resp.status_code == 403  # no Authorization header at all


def test_update_my_profile(client, auth_headers):
    resp = client.put(
        "/users/me",
        json={"full_name": "Asha Rao", "college": "IIT Bombay", "graduation_year": 2027},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_name"] == "Asha Rao"
    assert body["college"] == "IIT Bombay"
    assert body["graduation_year"] == 2027


def test_list_roles(client):
    resp = client.get("/roles")
    assert resp.status_code == 200
    names = {role["name"] for role in resp.json()}
    assert "SDE" in names
    assert "Data Analyst" in names


def test_select_role(client, auth_headers):
    roles = client.get("/roles").json()
    sde_id = next(role["id"] for role in roles if role["name"] == "SDE")

    resp = client.post("/users/me/role", json={"role_id": sde_id}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["target_role_id"] == sde_id


def test_select_unknown_role_404s(client, auth_headers):
    resp = client.post("/users/me/role", json={"role_id": 999999}, headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("path", "payload", "list_field"),
    [
        (
            "education",
            {"institution": "IIT Bombay", "degree": "B.Tech", "field_of_study": "CS", "start_year": 2023, "end_year": 2027},
            "institution",
        ),
        ("skills", {"name": "SQL", "proficiency": "intermediate"}, "name"),
        ("projects", {"title": "Placement Tracker", "description": "A tracker app", "tech_stack": "Kotlin"}, "title"),
        ("experience", {"company": "Acme Corp", "title": "Intern"}, "company"),
        ("certifications", {"name": "AWS Cloud Practitioner", "issuer": "AWS"}, "name"),
    ],
)
def test_add_and_list_profile_subresource(client, auth_headers, path, payload, list_field):
    create_resp = client.post(f"/users/me/{path}", json=payload, headers=auth_headers)
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created[list_field] == payload[list_field]
    assert "id" in created

    list_resp = client.get(f"/users/me/{path}", headers=auth_headers)
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert len(items) == 1
    assert items[0][list_field] == payload[list_field]


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("education", {"institution": "IIT Bombay", "degree": "B.Tech"}),
        ("skills", {"name": "SQL"}),
        ("projects", {"title": "Placement Tracker"}),
        ("experience", {"company": "Acme Corp"}),
        ("certifications", {"name": "AWS Cloud Practitioner"}),
    ],
)
def test_delete_profile_subresource(client, auth_headers, path, payload):
    created = client.post(f"/users/me/{path}", json=payload, headers=auth_headers).json()

    resp = client.delete(f"/users/me/{path}/{created['id']}", headers=auth_headers)
    assert resp.status_code == 204

    items = client.get(f"/users/me/{path}", headers=auth_headers).json()
    assert items == []


def test_delete_profile_subresource_not_found(client, auth_headers):
    resp = client.delete("/users/me/skills/999999", headers=auth_headers)
    assert resp.status_code == 404


def test_delete_profile_subresource_isolated_per_user(client, auth_headers):
    created = client.post("/users/me/skills", json={"name": "Python"}, headers=auth_headers).json()

    client.post("/auth/signup", json={"email": "other-profile@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-profile@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.delete(
        f"/users/me/skills/{created['id']}", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404

    items = client.get("/users/me/skills", headers=auth_headers).json()
    assert len(items) == 1


def test_profile_subresources_isolated_per_user(client):
    client.post("/auth/signup", json={"email": "a@example.com", "password": "supersecret123"})
    token_a = client.post(
        "/auth/login", json={"email": "a@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    client.post("/auth/signup", json={"email": "b@example.com", "password": "supersecret123"})
    token_b = client.post(
        "/auth/login", json={"email": "b@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    client.post(
        "/users/me/skills",
        json={"name": "Python"},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    b_skills = client.get(
        "/users/me/skills", headers={"Authorization": f"Bearer {token_b}"}
    ).json()
    assert b_skills == []
