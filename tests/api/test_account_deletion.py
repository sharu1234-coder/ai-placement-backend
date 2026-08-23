def _signup_and_login(client, email: str, password: str = "supersecret123") -> dict:
    client.post("/auth/signup", json={"email": email, "password": password})
    token = client.post("/auth/login", json={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_delete_account_requires_auth(client):
    resp = client.request("DELETE", "/users/me", json={"password": "supersecret123"})
    assert resp.status_code == 403


def test_delete_account_wrong_password_returns_403(client):
    headers = _signup_and_login(client, "wrongpass@example.com")

    resp = client.request("DELETE", "/users/me", json={"password": "not-the-password"}, headers=headers)
    assert resp.status_code == 403

    # Account must still exist -- a wrong-password attempt is a no-op, not a partial delete.
    assert client.get("/users/me", headers=headers).status_code == 200


def test_delete_account_success(client):
    headers = _signup_and_login(client, "delete-flow@example.com")

    client.put("/users/me", json={"full_name": "Jane Doe"}, headers=headers)
    client.post("/users/me/skills", json={"name": "Python"}, headers=headers)

    resp = client.request(
        "DELETE", "/users/me", json={"password": "supersecret123"}, headers=headers
    )
    assert resp.status_code == 204

    # The access token that just deleted the account must stop working immediately --
    # the user row it was issued for no longer exists.
    assert client.get("/users/me", headers=headers).status_code == 401

    # Deleting again (replay) 404s/401s rather than silently succeeding twice.
    resp2 = client.request("DELETE", "/users/me", json={"password": "supersecret123"}, headers=headers)
    assert resp2.status_code == 401


def test_delete_account_frees_email_for_resignup(client):
    email = "reuse-me@example.com"
    headers = _signup_and_login(client, email)

    client.request("DELETE", "/users/me", json={"password": "supersecret123"}, headers=headers)

    resp = client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    assert resp.status_code == 201
