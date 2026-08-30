import hashlib
import hmac
import json

from app.core import app_check
from app.core.config import settings


class _FakeAppCheckModule:
    """Stands in for firebase_admin.app_check -- see app_check.py's docstring for why the
    real SDK is never touched in tests."""

    def __init__(self, should_succeed: bool):
        self.should_succeed = should_succeed

    def verify_token(self, token):
        if not self.should_succeed:
            raise ValueError("invalid token")
        return {"sub": "test-app-id"}


def test_disabled_by_default_allows_requests_without_any_header(client):
    # settings.firebase_app_check_enabled defaults to False for every test (nothing in this
    # file's fixtures touches it) -- this is regression coverage that adding the dependency
    # to every router in app/main.py didn't accidentally start requiring the header today.
    response = client.get("/subscriptions/plans")
    assert response.status_code == 200


def test_enabled_rejects_a_request_with_no_app_check_header(client, monkeypatch):
    monkeypatch.setattr(settings, "firebase_app_check_enabled", True)

    response = client.get("/subscriptions/plans")

    assert response.status_code == 401


def test_enabled_rejects_a_request_with_an_invalid_app_check_token(client, monkeypatch):
    monkeypatch.setattr(settings, "firebase_app_check_enabled", True)
    monkeypatch.setattr(app_check, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(app_check, "_get_app_check_module", lambda: _FakeAppCheckModule(should_succeed=False))

    response = client.get("/subscriptions/plans", headers={"X-Firebase-AppCheck": "forged"})

    assert response.status_code == 401


def test_enabled_accepts_a_request_with_a_valid_app_check_token(client, monkeypatch):
    monkeypatch.setattr(settings, "firebase_app_check_enabled", True)
    monkeypatch.setattr(app_check, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(app_check, "_get_app_check_module", lambda: _FakeAppCheckModule(should_succeed=True))

    response = client.get("/subscriptions/plans", headers={"X-Firebase-AppCheck": "genuine"})

    assert response.status_code == 200


def test_legal_routes_are_exempt_even_when_app_check_is_enabled(client, monkeypatch):
    # Public policy documents -- meant to be readable from a plain browser link, not just
    # the real Android app. See app/main.py's include_router() call for legal_router.
    monkeypatch.setattr(settings, "firebase_app_check_enabled", True)

    response = client.get("/legal/privacy-policy")

    assert response.status_code == 200


def test_razorpay_webhook_route_is_exempt_even_when_app_check_is_enabled(client, monkeypatch):
    """Razorpay's own servers call this route directly -- they will never send an
    X-Firebase-AppCheck header, so it must stay reachable regardless of this setting (see
    the per-route dependency list in app/api/subscriptions.py, which deliberately skips
    /webhook)."""
    monkeypatch.setattr(settings, "firebase_app_check_enabled", True)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "fake_webhook_secret")

    entity = {"id": "x", "order_id": "order_never_created"}
    body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": entity}}}).encode()
    signature = hmac.new(b"fake_webhook_secret", body, hashlib.sha256).hexdigest()

    response = client.post(
        "/subscriptions/webhook",
        content=body,
        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"},
        # deliberately no X-Firebase-AppCheck header
    )

    # 200 (unknown-order no-op), not 401 -- proves the App Check dependency was never
    # evaluated for this route at all, not just that it happened to pass.
    assert response.status_code == 200
