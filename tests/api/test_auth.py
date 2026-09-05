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
    assert resp.status_code == 401


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


def test_authentication_and_authorization_failures_use_distinct_status_codes(client):
    """Locks in the 401-vs-403 split so neither can drift back into the other.

    These mean different things and clients act on them differently: Android's auth
    interceptor retries a 401 once with a refreshed token and, if that fails, tells the
    student their session expired and sends them to sign in (see ErrorMessages.kt). Doing
    that in response to a *permission* denial would sign out a perfectly valid session; the
    reverse -- reporting a missing token as "forbidden" -- tells a signed-out student they
    are not allowed in rather than that they need to sign in.

    This distinction was previously blurred: FastAPI's HTTPBearer used to answer a missing
    Authorization header with 403, so a whole class of "not signed in" responses arrived
    labelled as "signed in but not permitted", disagreeing with this app's own handler for an
    invalid token (401) two lines away in app/api/deps.py. The dependency upgrade corrected
    the missing-credentials case to 401 (RFC 7235), which is why ~19 *_requires_auth tests
    moved. Both halves are asserted together here so a future change cannot quietly collapse
    them again.
    """
    # No credentials at all -> 401, not 403.
    assert client.get("/users/me").status_code == 401

    # Credentials present but unusable -> also 401. Same "who are you?" failure.
    assert client.get("/users/me", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401

    # Authenticated successfully, then denied on the merits -> 403, and it must NOT be 401.
    client.post("/auth/signup", json={"email": "authz@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "authz@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    denied = client.request("DELETE", "/users/me", json={"password": "wrong-password"}, headers=headers)
    assert denied.status_code == 403

    # ...and the account survives the denied attempt.
    assert client.get("/users/me", headers=headers).status_code == 200
