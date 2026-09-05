import hashlib
import hmac

import httpx
import pytest

from app.core.config import settings
from app.services import subscription_service as svc


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "twin@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "twin@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _grant_premium(client, monkeypatch, auth_headers):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake_key_id")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake_test_secret")
    fake_request = httpx.Request("POST", svc.RAZORPAY_ORDERS_URL)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: httpx.Response(200, json={"id": "order_twin", "status": "created"}, request=fake_request),
    )
    checkout = client.post("/subscriptions/checkout", json={"plan": "1_month"}, headers=auth_headers).json()
    signature = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        f"{checkout['razorpay_order_id']}|pay_twin".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    verify = client.post(
        "/subscriptions/verify",
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": "pay_twin",
            "razorpay_signature": signature,
        },
        headers=auth_headers,
    )
    assert verify.status_code == 200 and verify.json()["tier"] == "premium"


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
    assert response.status_code == 401  # no bearer token supplied at all


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


def test_reasons_are_locked_behind_premium_server_side(client, auth_headers, monkeypatch):
    """Before this fix, GET /career-twin sent the real evidence-breakdown `reasons` to every
    authenticated user regardless of subscription status -- Android's PaywallGate only ever
    hid the UI, so a free user's own direct API call already had full "premium" content.
    A non-empty locked placeholder (not []) is intentional: it's what keeps the client's own
    `if (reasons.isNotEmpty())` check still showing the upgrade prompt for free users."""
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    free_body = client.get("/career-twin", headers=auth_headers).json()
    assert len(free_body["reasons"]) == 1
    assert "premium" in free_body["reasons"][0].lower()
    assert "skills are weighted for this role" not in free_body["reasons"][0]

    # The /readiness projection must gate the same field the same way, while still free.
    free_readiness = client.get("/career-twin/readiness", headers=auth_headers).json()
    assert "premium" in free_readiness["reasons"][0].lower()

    _grant_premium(client, monkeypatch, auth_headers)
    premium_body = client.get("/career-twin", headers=auth_headers).json()
    assert any("skills are weighted for this role" in r for r in premium_body["reasons"])


def test_career_twin_is_isolated_per_profile(client, auth_headers, other_auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    mine = client.get("/career-twin", headers=auth_headers).json()
    theirs = client.get("/career-twin", headers=other_auth_headers).json()
    assert mine["has_role"] is True
    assert theirs["has_role"] is False
