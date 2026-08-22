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
