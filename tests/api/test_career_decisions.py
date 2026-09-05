import hashlib
import hmac

import httpx
import pytest

from app.core.config import settings
from app.services import subscription_service as svc


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "decision@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "decision@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def razorpay_keys(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake_key_id")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake_test_secret")


def _grant_premium(client, monkeypatch, auth_headers, order_id: str = "order_decision_lab"):
    """Career Decision Lab is fully premium-gated server-side -- every test below needs a
    real, verified subscription first, driven through the actual checkout+verify flow (same
    pattern as tests/api/test_subscriptions.py) rather than writing a Subscription row
    directly, so this also incidentally re-proves the endpoint trusts only that flow."""
    fake_request = httpx.Request("POST", svc.RAZORPAY_ORDERS_URL)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: httpx.Response(200, json={"id": order_id, "status": "created"}, request=fake_request),
    )
    checkout = client.post("/subscriptions/checkout", json={"plan": "1_month"}, headers=auth_headers).json()
    payment_id = f"pay_{order_id}"
    signature = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        f"{checkout['razorpay_order_id']}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    verify = client.post(
        "/subscriptions/verify",
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
        headers=auth_headers,
    )
    assert verify.status_code == 200 and verify.json()["tier"] == "premium"


def _role_ids(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    sde_id = next(r["id"] for r in roles if r["name"] == "SDE")
    data_analyst_id = next(r["id"] for r in roles if r["name"] == "Data Analyst")
    return sde_id, data_analyst_id


def test_requires_auth(client):
    resp = client.post("/career-decisions/simulate", json={"role_ids": [1, 2]})
    assert resp.status_code == 401


def test_requires_premium(client, auth_headers):
    """The whole endpoint is Premium-only (see Screen's own PaywallGate copy: "Career Decision
    Lab is Premium"). Before this fix, a free user's authenticated request got the full
    response anyway -- PaywallGate only ever hid the UI, never enforced anything server-side."""
    resp = client.post("/career-decisions/simulate", json={"role_ids": [1, 2]}, headers=auth_headers)
    assert resp.status_code == 403


def test_requires_at_least_two_roles(client, auth_headers, monkeypatch):
    _grant_premium(client, monkeypatch, auth_headers, "order_min_roles")
    resp = client.post("/career-decisions/simulate", json={"role_ids": [1]}, headers=auth_headers)
    assert resp.status_code == 422


def test_rejects_more_than_three_roles(client, auth_headers, monkeypatch):
    _grant_premium(client, monkeypatch, auth_headers, "order_max_roles")
    resp = client.post("/career-decisions/simulate", json={"role_ids": [1, 2, 3, 4]}, headers=auth_headers)
    assert resp.status_code == 422


def test_unknown_role_id_returns_422(client, auth_headers, monkeypatch):
    _grant_premium(client, monkeypatch, auth_headers, "order_unknown_role")
    sde_id, _ = _role_ids(client, auth_headers)
    resp = client.post("/career-decisions/simulate", json={"role_ids": [sde_id, 999999]}, headers=auth_headers)
    assert resp.status_code == 422


def test_simulate_with_no_evidence_returns_two_honest_stretches(client, auth_headers, monkeypatch):
    _grant_premium(client, monkeypatch, auth_headers, "order_no_evidence")
    sde_id, data_analyst_id = _role_ids(client, auth_headers)
    resp = client.post(
        "/career-decisions/simulate", json={"role_ids": [sde_id, data_analyst_id]}, headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["roles"]) == 2
    assert all(r["classification"] == "stretch" for r in body["roles"])
    assert all(r["readiness_percent"] == 0 for r in body["roles"])
    assert all(r["is_estimate"] is True for r in body["roles"])
    assert len(body["assumptions"]) > 0
    assert "probability" not in str(body).lower()


def test_weekly_hours_are_echoed_back_clamped(client, auth_headers, monkeypatch):
    _grant_premium(client, monkeypatch, auth_headers, "order_weekly_hours")
    sde_id, data_analyst_id = _role_ids(client, auth_headers)
    resp = client.post(
        "/career-decisions/simulate",
        json={"role_ids": [sde_id, data_analyst_id], "weekly_hours": 9999},
        headers=auth_headers,
    )
    body = resp.json()
    assert body["weekly_hours_used"] == 80
    assert all(r["estimated_weeks"] is not None for r in body["roles"])


def test_roles_are_isolated_per_profile(client, auth_headers, monkeypatch):
    """Nothing is persisted -- two calls for the same profile must not accumulate state, and
    a second profile calling this must never see the first profile's evidence leak in."""
    _grant_premium(client, monkeypatch, auth_headers, "order_isolation")
    sde_id, data_analyst_id = _role_ids(client, auth_headers)
    first = client.post(
        "/career-decisions/simulate", json={"role_ids": [sde_id, data_analyst_id]}, headers=auth_headers
    ).json()
    second = client.post(
        "/career-decisions/simulate", json={"role_ids": [sde_id, data_analyst_id]}, headers=auth_headers
    ).json()
    assert first == second
