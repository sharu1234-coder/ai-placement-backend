def test_signup_and_login(client):
    signup_resp = client.post(
        "/auth/signup", json={"email": "student@example.com", "password": "supersecret123"}
    )
    assert signup_resp.status_code == 201
    body = signup_resp.json()
    assert body["email"] == "student@example.com"
    assert "password_hash" not in body
    assert "password" not in body

    login_resp = client.post(
        "/auth/login", json={"email": "student@example.com", "password": "supersecret123"}
    )
    assert login_resp.status_code == 200
    tokens = login_resp.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]


def test_signup_duplicate_email(client):
    payload = {"email": "dup@example.com", "password": "supersecret123"}
    client.post("/auth/signup", json=payload)
    resp = client.post("/auth/signup", json=payload)
    assert resp.status_code == 409


def test_signup_password_too_short(client):
    resp = client.post("/auth/signup", json={"email": "short@example.com", "password": "abc"})
    assert resp.status_code == 422


def test_login_wrong_password(client):
    client.post("/auth/signup", json={"email": "wrong@example.com", "password": "supersecret123"})
    resp = client.post("/auth/login", json={"email": "wrong@example.com", "password": "bad-password"})
    assert resp.status_code == 401


def test_login_unknown_email(client):
    resp = client.post("/auth/login", json={"email": "nobody@example.com", "password": "supersecret123"})
    assert resp.status_code == 401


def test_refresh_token_flow(client):
    client.post("/auth/signup", json={"email": "refresh@example.com", "password": "supersecret123"})
    login_resp = client.post(
        "/auth/login", json={"email": "refresh@example.com", "password": "supersecret123"}
    )
    refresh_token = login_resp.json()["refresh_token"]

    resp = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_refresh_rejects_access_token(client):
    client.post("/auth/signup", json={"email": "swap@example.com", "password": "supersecret123"})
    login_resp = client.post(
        "/auth/login", json={"email": "swap@example.com", "password": "supersecret123"}
    )
    access_token = login_resp.json()["access_token"]

    resp = client.post("/auth/refresh", json={"refresh_token": access_token})
    assert resp.status_code == 401


def test_refresh_rejects_garbage_token(client):
    resp = client.post("/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401


def test_logout_requires_auth(client):
    resp = client.post("/auth/logout")
    assert resp.status_code == 403


def test_logout_invalidates_existing_access_token(client):
    client.post("/auth/signup", json={"email": "logout@example.com", "password": "supersecret123"})
    login_resp = client.post(
        "/auth/login", json={"email": "logout@example.com", "password": "supersecret123"}
    )
    headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

    # Token works before logout (404 -- not 401/403 -- proves it authenticated fine).
    assert client.get("/resumes/999999/analysis", headers=headers).status_code == 404

    assert client.post("/auth/logout", headers=headers).status_code == 200

    assert client.get("/resumes/999999/analysis", headers=headers).status_code == 401


def test_logout_invalidates_existing_refresh_token(client):
    client.post(
        "/auth/signup", json={"email": "logout-refresh@example.com", "password": "supersecret123"}
    )
    login_resp = client.post(
        "/auth/login", json={"email": "logout-refresh@example.com", "password": "supersecret123"}
    )
    tokens = login_resp.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    client.post("/auth/logout", headers=headers)

    resp = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401


def test_login_rate_limited_after_repeated_failures(client):
    client.post("/auth/signup", json={"email": "bruteforce@example.com", "password": "supersecret123"})

    for _ in range(5):
        resp = client.post(
            "/auth/login", json={"email": "bruteforce@example.com", "password": "wrong-password"}
        )
        assert resp.status_code == 401

    blocked = client.post(
        "/auth/login", json={"email": "bruteforce@example.com", "password": "wrong-password"}
    )
    assert blocked.status_code == 429


def test_signup_rate_limited(client):
    for i in range(3):
        resp = client.post(
            "/auth/signup", json={"email": f"ratelimited{i}@example.com", "password": "supersecret123"}
        )
        assert resp.status_code == 201

    blocked = client.post(
        "/auth/signup", json={"email": "ratelimited-extra@example.com", "password": "supersecret123"}
    )
    assert blocked.status_code == 429


def test_forgot_password_rate_limited(client):
    for _ in range(3):
        resp = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
        assert resp.status_code == 200

    blocked = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert blocked.status_code == 429


def test_reset_password_rate_limited(client):
    for _ in range(10):
        resp = client.post(
            "/auth/reset-password",
            json={"email": "nobody@example.com", "code": "deadbeef", "new_password": "new-password456"},
        )
        assert resp.status_code == 400

    blocked = client.post(
        "/auth/reset-password",
        json={"email": "nobody@example.com", "code": "deadbeef", "new_password": "new-password456"},
    )
    assert blocked.status_code == 429


def test_refresh_rate_limited(client):
    for _ in range(20):
        resp = client.post("/auth/refresh", json={"refresh_token": "not-a-real-token"})
        assert resp.status_code == 401

    blocked = client.post("/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert blocked.status_code == 429
