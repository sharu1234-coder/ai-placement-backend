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


def test_get_my_profile_includes_email(client, auth_headers):
    # email lives on User, not StudentProfile -- regression test for profile_read()
    # sourcing it explicitly rather than relying on bare from_attributes=True.
    resp = client.get("/users/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == "profile@example.com"


def test_update_my_profile_all_real_fields_round_trip(client, auth_headers):
    # Regression test for the "profile save doesn't persist" bug: every field the PUT
    # schema actually declares must both save and be readable back via a fresh GET.
    payload = {
        "full_name": "Asha Rao",
        "mobile_number": "+91 98765 43210",
        "college": "IIT Bombay",
        "graduation_year": 2027,
        "degree": "B.Tech",
        "branch": "Computer Science",
        "experience_level": "beginner",
        "preparation_goals": ["dsa", "sql"],
    }
    put_resp = client.put("/users/me", json=payload, headers=auth_headers)
    assert put_resp.status_code == 200
    for key, value in payload.items():
        assert put_resp.json()[key] == value

    get_resp = client.get("/users/me", headers=auth_headers)
    assert get_resp.status_code == 200
    for key, value in payload.items():
        assert get_resp.json()[key] == value


def test_update_my_profile_rejects_overlong_mobile_number(client, auth_headers):
    resp = client.put("/users/me", json={"mobile_number": "1" * 21}, headers=auth_headers)
    assert resp.status_code == 422


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


def test_update_my_profile_onboarding_fields(client, auth_headers):
    resp = client.put(
        "/users/me",
        json={
            "degree": "B.Tech",
            "branch": "Computer Science",
            "experience_level": "beginner",
            "preparation_goals": ["dsa", "sql", "mock_interviews"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["degree"] == "B.Tech"
    assert body["branch"] == "Computer Science"
    assert body["experience_level"] == "beginner"
    assert body["preparation_goals"] == ["dsa", "sql", "mock_interviews"]
    assert body["onboarding_completed"] is False


def test_update_my_profile_rejects_invalid_experience_level(client, auth_headers):
    resp = client.put("/users/me", json={"experience_level": "expert"}, headers=auth_headers)
    assert resp.status_code == 422


def test_update_my_profile_rejects_overlong_full_name(client, auth_headers):
    # StudentProfile.full_name is String(255) -- an overlong value must 422 here, not hit an
    # unhandled DB error on Postgres.
    resp = client.put("/users/me", json={"full_name": "x" * 256}, headers=auth_headers)
    assert resp.status_code == 422


def test_update_my_profile_accepts_full_name_at_the_length_limit(client, auth_headers):
    resp = client.put("/users/me", json={"full_name": "x" * 255}, headers=auth_headers)
    assert resp.status_code == 200


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


def test_select_custom_role_rejects_overlong_value(client, auth_headers):
    # StudentProfile.custom_target_role is String(150) -- an overlong value must 422 here,
    # not hit an unhandled DB error on Postgres.
    resp = client.post("/users/me/role", json={"custom_role": "x" * 151}, headers=auth_headers)
    assert resp.status_code == 422


def test_select_custom_role_clears_catalog_role(client, auth_headers):
    roles = client.get("/roles").json()
    sde_id = next(role["id"] for role in roles if role["name"] == "SDE")
    client.post("/users/me/role", json={"role_id": sde_id}, headers=auth_headers)

    resp = client.post(
        "/users/me/role", json={"custom_role": "Bioinformatics Engineer"}, headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["custom_target_role"] == "Bioinformatics Engineer"
    assert body["target_role_id"] is None


def test_select_catalog_role_clears_custom_role(client, auth_headers):
    client.post("/users/me/role", json={"custom_role": "Bioinformatics Engineer"}, headers=auth_headers)

    roles = client.get("/roles").json()
    sde_id = next(role["id"] for role in roles if role["name"] == "SDE")
    resp = client.post("/users/me/role", json={"role_id": sde_id}, headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["target_role_id"] == sde_id
    assert body["custom_target_role"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"role_id": None, "custom_role": None},
    ],
)
def test_select_role_rejects_neither_set(client, auth_headers, payload):
    resp = client.post("/users/me/role", json=payload, headers=auth_headers)
    assert resp.status_code == 422


def test_select_role_rejects_both_set(client, auth_headers):
    roles = client.get("/roles").json()
    sde_id = next(role["id"] for role in roles if role["name"] == "SDE")
    resp = client.post(
        "/users/me/role",
        json={"role_id": sde_id, "custom_role": "Bioinformatics Engineer"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_select_secondary_role(client, auth_headers):
    roles = client.get("/roles").json()
    sde_id = next(role["id"] for role in roles if role["name"] == "SDE")
    data_analyst_id = next(role["id"] for role in roles if role["name"] == "Data Analyst")

    client.post("/users/me/role", json={"role_id": sde_id}, headers=auth_headers)
    resp = client.post(
        "/users/me/role/secondary", json={"role_id": data_analyst_id}, headers=auth_headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["target_role_id"] == sde_id
    assert body["secondary_target_role_id"] == data_analyst_id


def test_select_unknown_secondary_role_404s(client, auth_headers):
    resp = client.post("/users/me/role/secondary", json={"role_id": 999999}, headers=auth_headers)
    assert resp.status_code == 404


def test_onboarding_complete_is_idempotent_and_reflected_on_get(client, auth_headers):
    initial = client.get("/users/me", headers=auth_headers).json()
    assert initial["onboarding_completed"] is False

    first = client.post("/users/me/onboarding/complete", headers=auth_headers)
    assert first.status_code == 200
    assert first.json()["onboarding_completed"] is True

    second = client.post("/users/me/onboarding/complete", headers=auth_headers)
    assert second.status_code == 200
    assert second.json()["onboarding_completed"] is True

    after = client.get("/users/me", headers=auth_headers).json()
    assert after["onboarding_completed"] is True


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


def test_add_project_rejects_overlong_description(client, auth_headers):
    """Real audit gap, now fixed: description/tech_stack previously had no max_length at
    all, unlike every other free-text AI-input field in the app -- this content reaches
    interview_service's question-generation prompt verbatim via {projects}."""
    resp = client.post(
        "/users/me/projects",
        json={"title": "A project", "description": "x" * 2001},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_add_project_rejects_overlong_tech_stack(client, auth_headers):
    resp = client.post(
        "/users/me/projects",
        json={"title": "A project", "tech_stack": "x" * 301},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_add_project_accepts_description_at_the_length_limit(client, auth_headers):
    resp = client.post(
        "/users/me/projects",
        json={"title": "A project", "description": "x" * 2000, "tech_stack": "x" * 300},
        headers=auth_headers,
    )
    assert resp.status_code == 201


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
