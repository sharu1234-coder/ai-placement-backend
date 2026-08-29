import hashlib
import hmac

import httpx
import pytest

from app.core.config import settings
from app.services import subscription_service as svc


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "sub@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "sub@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "sub2@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "sub2@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def razorpay_keys(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake_key_id")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake_test_secret")


def _fake_order(monkeypatch, order_id: str):
    fake_request = httpx.Request("POST", svc.RAZORPAY_ORDERS_URL)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: httpx.Response(200, json={"id": order_id, "status": "created"}, request=fake_request),
    )


def _sign(order_id: str, payment_id: str) -> str:
    return hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def test_plans_are_server_authoritative(client, auth_headers):
    response = client.get("/subscriptions/plans")
    assert response.status_code == 200
    plans = {p["plan"]: p for p in response.json()}
    assert plans["1_month"]["amount_paise"] == 19_900
    assert plans["3_months"]["amount_paise"] == 49_900
    assert plans["6_months"]["amount_paise"] == 79_900
    assert plans["1_year"]["amount_paise"] == 99_900


def test_status_requires_auth(client):
    response = client.get("/subscriptions/status")
    assert response.status_code == 403


def test_status_defaults_to_free_never_subscribed(client, auth_headers):
    response = client.get("/subscriptions/status", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"tier": "free", "status": "never_subscribed", "plan": None, "expires_at": None}


def test_checkout_never_accepts_a_client_supplied_amount(client, auth_headers, monkeypatch):
    """The request schema only has a `plan` field -- passing `amount` in the body is simply
    ignored by Pydantic, which is itself the guarantee: there is no code path that reads a
    price from the request."""
    _fake_order(monkeypatch, "order_amt_test")
    response = client.post(
        "/subscriptions/checkout",
        json={"plan": "1_month", "amount_paise": 1},
        headers=auth_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["amount_paise"] == 19_900
    assert "razorpay_key_secret" not in body


def test_full_checkout_and_verify_flow_activates_premium(client, auth_headers, monkeypatch):
    _fake_order(monkeypatch, "order_flow_1")
    checkout = client.post("/subscriptions/checkout", json={"plan": "1_month"}, headers=auth_headers).json()

    signature = _sign(checkout["razorpay_order_id"], "pay_flow_1")
    verify = client.post(
        "/subscriptions/verify",
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": "pay_flow_1",
            "razorpay_signature": signature,
        },
        headers=auth_headers,
    )
    assert verify.status_code == 200
    assert verify.json()["tier"] == "premium"
    assert verify.json()["status"] == "active"

    status = client.get("/subscriptions/status", headers=auth_headers)
    assert status.json()["tier"] == "premium"


def test_tampered_signature_does_not_activate_premium(client, auth_headers, monkeypatch):
    _fake_order(monkeypatch, "order_tamper_1")
    checkout = client.post("/subscriptions/checkout", json={"plan": "1_month"}, headers=auth_headers).json()

    verify = client.post(
        "/subscriptions/verify",
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": "pay_tamper_1",
            "razorpay_signature": "0" * 64,
        },
        headers=auth_headers,
    )
    assert verify.status_code == 400

    status = client.get("/subscriptions/status", headers=auth_headers)
    assert status.json()["tier"] == "free"


def test_duplicate_verify_callback_is_idempotent(client, auth_headers, monkeypatch):
    _fake_order(monkeypatch, "order_dup_1")
    checkout = client.post("/subscriptions/checkout", json={"plan": "1_month"}, headers=auth_headers).json()
    signature = _sign(checkout["razorpay_order_id"], "pay_dup_1")
    payload = {
        "razorpay_order_id": checkout["razorpay_order_id"],
        "razorpay_payment_id": "pay_dup_1",
        "razorpay_signature": signature,
    }

    first = client.post("/subscriptions/verify", json=payload, headers=auth_headers)
    second = client.post("/subscriptions/verify", json=payload, headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["expires_at"] == second.json()["expires_at"]


def test_another_users_order_cannot_be_verified_by_a_different_account(client, auth_headers, other_auth_headers, monkeypatch):
    _fake_order(monkeypatch, "order_isolated_1")
    checkout = client.post("/subscriptions/checkout", json={"plan": "1_month"}, headers=auth_headers).json()
    signature = _sign(checkout["razorpay_order_id"], "pay_isolated_1")

    response = client.post(
        "/subscriptions/verify",
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": "pay_isolated_1",
            "razorpay_signature": signature,
        },
        headers=other_auth_headers,
    )
    # 404, not 403 -- never confirms/denies that the order id exists for someone else.
    assert response.status_code == 404

    victim_status = client.get("/subscriptions/status", headers=auth_headers)
    assert victim_status.json()["tier"] == "free"
