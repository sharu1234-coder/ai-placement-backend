def test_get_privacy_policy_is_public(client):
    resp = client.get("/legal/privacy-policy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "1"
    assert "AI Placement OS" in body["content"]
    assert "privacy@sgldquantlabs.com" in body["content"]


def test_get_privacy_policy_does_not_require_auth(client):
    # No Authorization header at all -- must not 401/403 like the account endpoints do.
    resp = client.get("/legal/privacy-policy")
    assert resp.status_code == 200


def test_get_terms_of_service_is_public(client):
    resp = client.get("/legal/terms-of-service")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "1"
    assert "AI Placement OS" in body["content"]
    assert "legal@sgldquantlabs.com" in body["content"]


def test_get_terms_of_service_does_not_require_auth(client):
    resp = client.get("/legal/terms-of-service")
    assert resp.status_code == 200
