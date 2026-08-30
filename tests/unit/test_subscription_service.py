import datetime
import hashlib
import hmac

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.database.base import Base
from app.models.profile import StudentProfile  # noqa: F401  (registers the FK target table)
from app.models.subscription import Subscription
from app.services import subscription_service as svc

PROFILE_ID = 1
OTHER_PROFILE_ID = 2


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def razorpay_keys(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake_key_id")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake_test_secret")


def _fake_order_response(order_id: str = "order_fake123") -> httpx.Response:
    return httpx.Response(200, json={"id": order_id, "status": "created"}, request=httpx.Request("POST", svc.RAZORPAY_ORDERS_URL))


def _sign(order_id: str, payment_id: str) -> str:
    return hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class TestCreateOrder:
    def test_rejects_unknown_plan(self, db):
        with pytest.raises(svc.UnknownPlanError):
            svc.create_order(db, PROFILE_ID, "lifetime")

    def test_raises_when_razorpay_not_configured(self, db, monkeypatch):
        monkeypatch.setattr(settings, "razorpay_key_id", None)
        with pytest.raises(svc.RazorpayNotConfiguredError):
            svc.create_order(db, PROFILE_ID, "1_month")

    def test_creates_order_with_server_side_amount(self, db, monkeypatch):
        captured = {}

        def fake_post(url, auth=None, json=None, timeout=None):
            captured["json"] = json
            captured["auth"] = auth
            return _fake_order_response()

        monkeypatch.setattr(httpx, "post", fake_post)

        subscription = svc.create_order(db, PROFILE_ID, "3_months")

        assert subscription.razorpay_order_id == "order_fake123"
        assert subscription.amount_paise == svc.PLAN_CATALOG["3_months"]["amount_paise"]
        assert subscription.status == "created"
        # The amount actually sent to Razorpay came from PLAN_CATALOG, never a client input --
        # this test has no way to pass a client amount at all, which is the real guarantee.
        assert captured["json"]["amount"] == svc.PLAN_CATALOG["3_months"]["amount_paise"]
        assert captured["auth"] == ("rzp_test_fake_key_id", "fake_test_secret")

    def test_raises_on_razorpay_api_error(self, db, monkeypatch):
        fake_request = httpx.Request("POST", svc.RAZORPAY_ORDERS_URL)
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **k: httpx.Response(401, json={"error": "bad key"}, request=fake_request),
        )
        with pytest.raises(svc.RazorpayApiError):
            svc.create_order(db, PROFILE_ID, "1_month")


class TestVerifyPayment:
    def _create(self, db, monkeypatch, plan="1_month", order_id="order_abc") -> Subscription:
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_order_response(order_id))
        return svc.create_order(db, PROFILE_ID, plan)

    def test_valid_signature_activates_premium(self, db, monkeypatch):
        subscription = self._create(db, monkeypatch)
        signature = _sign(subscription.razorpay_order_id, "pay_valid1")

        result = svc.verify_payment(db, PROFILE_ID, subscription.razorpay_order_id, "pay_valid1", signature)

        assert result.status == "paid"
        assert result.razorpay_payment_id == "pay_valid1"
        assert result.expires_at is not None
        expected_days = svc.PLAN_CATALOG["1_month"]["duration_days"]
        assert (result.expires_at - result.starts_at).days == expected_days

    def test_raises_if_razorpay_becomes_unconfigured_between_order_creation_and_verification(self, db, monkeypatch):
        subscription = self._create(db, monkeypatch)
        monkeypatch.setattr(settings, "razorpay_key_secret", None)

        with pytest.raises(svc.RazorpayNotConfiguredError):
            svc.verify_payment(db, PROFILE_ID, subscription.razorpay_order_id, "pay_x", "sig_x")

    def test_concurrent_duplicate_verify_loses_the_atomic_update_race_without_erroring(self, db, monkeypatch):
        """Genuinely simulates two near-simultaneous requests via two separate DB sessions on
        the same in-memory database (StaticPool): both load the row while it's still
        "created", session B commits first, then session A's own verify_payment call runs its
        conditional UPDATE (WHERE status != 'paid') against a row session A still believes is
        unpaid -- that UPDATE must affect zero rows and return the winner's row cleanly rather
        than raising or double-extending expiry."""
        subscription = self._create(db, monkeypatch)
        order_id = subscription.razorpay_order_id
        signature = _sign(order_id, "pay_race")

        session_b = sessionmaker(bind=db.get_bind())()
        try:
            winner = svc.verify_payment(session_b, PROFILE_ID, order_id, "pay_race", signature)
            winners_expiry = winner.expires_at
        finally:
            session_b.close()

        # session `db`'s in-memory copy of `subscription` is now stale (still "created") --
        # exactly the state a truly concurrent request would be in.
        result = svc.verify_payment(db, PROFILE_ID, order_id, "pay_race", signature)

        assert result.status == "paid"
        assert result.expires_at == winners_expiry

    def test_tampered_signature_is_rejected_and_never_activates_premium(self, db, monkeypatch):
        subscription = self._create(db, monkeypatch)

        with pytest.raises(svc.SignatureVerificationError):
            svc.verify_payment(db, PROFILE_ID, subscription.razorpay_order_id, "pay_valid1", "0" * 64)

        db.refresh(subscription)
        assert subscription.status == "failed"
        status = svc.get_status(db, PROFILE_ID)
        assert status["tier"] == "free"

    def test_wrong_order_id_is_rejected(self, db, monkeypatch):
        self._create(db, monkeypatch)
        with pytest.raises(svc.OrderNotFoundError):
            svc.verify_payment(db, PROFILE_ID, "order_does_not_exist", "pay_x", "irrelevant")

    def test_another_profiles_order_cannot_be_verified(self, db, monkeypatch):
        subscription = self._create(db, monkeypatch)
        signature = _sign(subscription.razorpay_order_id, "pay_valid1")

        # Same order id, but requested under a different profile -- ownership scoping, not
        # just "does this order id exist anywhere."
        with pytest.raises(svc.OrderNotFoundError):
            svc.verify_payment(db, OTHER_PROFILE_ID, subscription.razorpay_order_id, "pay_valid1", signature)

    def test_duplicate_verification_call_is_idempotent(self, db, monkeypatch):
        subscription = self._create(db, monkeypatch)
        signature = _sign(subscription.razorpay_order_id, "pay_valid1")

        first = svc.verify_payment(db, PROFILE_ID, subscription.razorpay_order_id, "pay_valid1", signature)
        second = svc.verify_payment(db, PROFILE_ID, subscription.razorpay_order_id, "pay_valid1", signature)

        assert first.expires_at == second.expires_at

    def test_same_payment_id_cannot_verify_a_different_order(self, db, monkeypatch):
        sub_a = self._create(db, monkeypatch, order_id="order_a")
        svc.verify_payment(db, PROFILE_ID, sub_a.razorpay_order_id, "pay_shared", _sign("order_a", "pay_shared"))

        sub_b = self._create(db, monkeypatch, order_id="order_b")
        with pytest.raises(svc.DuplicatePaymentError):
            svc.verify_payment(db, PROFILE_ID, sub_b.razorpay_order_id, "pay_shared", _sign("order_b", "pay_shared"))

    def test_renewal_before_expiry_stacks_onto_remaining_time_instead_of_resetting(self, db, monkeypatch):
        sub_a = self._create(db, monkeypatch, plan="1_month", order_id="order_a")
        svc.verify_payment(db, PROFILE_ID, sub_a.razorpay_order_id, "pay_a", _sign("order_a", "pay_a"))
        first_expiry = svc.get_status(db, PROFILE_ID)["expires_at"]

        sub_b = self._create(db, monkeypatch, plan="1_month", order_id="order_b")
        svc.verify_payment(db, PROFILE_ID, sub_b.razorpay_order_id, "pay_b", _sign("order_b", "pay_b"))
        second_expiry = svc.get_status(db, PROFILE_ID)["expires_at"]

        # Renewed while still active: new expiry should be ~1 month past the FIRST expiry,
        # not ~1 month past "now" -- i.e. strictly more than one plan-length beyond first_expiry
        # would be wrong, but landing close to first_expiry + 30 days is correct.
        assert second_expiry > first_expiry
        assert abs((second_expiry - first_expiry).days - svc.PLAN_CATALOG["1_month"]["duration_days"]) <= 1


class TestGetStatus:
    def test_never_subscribed_profile_is_free(self, db):
        status = svc.get_status(db, PROFILE_ID)
        assert status == {"tier": "free", "status": "never_subscribed", "plan": None, "expires_at": None}

    def test_active_subscription_is_premium(self, db, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_order_response("order_status_test"))
        subscription = svc.create_order(db, PROFILE_ID, "1_month")
        svc.verify_payment(
            db, PROFILE_ID, subscription.razorpay_order_id, "pay_1",
            _sign(subscription.razorpay_order_id, "pay_1"),
        )

        status = svc.get_status(db, PROFILE_ID)
        assert status["tier"] == "premium"
        assert status["status"] == "active"
        assert status["plan"] == "1_month"

    def test_expired_subscription_reports_free_expired_not_active(self, db):
        expired = Subscription(
            profile_id=PROFILE_ID,
            plan="1_month",
            amount_paise=19_900,
            currency="INR",
            razorpay_order_id="order_old",
            razorpay_payment_id="pay_old",
            status="paid",
            starts_at=datetime.datetime.utcnow() - datetime.timedelta(days=60),
            expires_at=datetime.datetime.utcnow() - datetime.timedelta(days=30),
            verified_at=datetime.datetime.utcnow() - datetime.timedelta(days=60),
        )
        db.add(expired)
        db.commit()

        status = svc.get_status(db, PROFILE_ID)
        assert status == {"tier": "free", "status": "expired", "plan": None, "expires_at": None}


class TestWebhook:
    def test_verify_webhook_signature_matches_the_documented_hmac_formula(self):
        body = b'{"event":"payment.captured"}'
        expected = hmac.new(b"whsecret", body, hashlib.sha256).hexdigest()

        assert svc.verify_webhook_signature(body, expected, "whsecret") is True
        assert svc.verify_webhook_signature(body, "0" * 64, "whsecret") is False
        assert svc.verify_webhook_signature(body, expected, "wrong-secret") is False

    def test_activate_from_webhook_activates_premium(self, db, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_order_response("order_wh_1"))
        subscription = svc.create_order(db, PROFILE_ID, "1_month")

        result = svc.activate_from_webhook(db, subscription.razorpay_order_id, "pay_wh_1")

        assert result is not None
        assert result.status == "paid"
        assert result.razorpay_payment_id == "pay_wh_1"
        assert svc.get_status(db, PROFILE_ID)["tier"] == "premium"

    def test_activate_from_webhook_for_unknown_order_returns_none_without_raising(self, db):
        result = svc.activate_from_webhook(db, "order_does_not_exist", "pay_x")
        assert result is None

    def test_activate_from_webhook_redelivery_is_idempotent(self, db, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_order_response("order_wh_dup"))
        subscription = svc.create_order(db, PROFILE_ID, "1_month")

        first = svc.activate_from_webhook(db, subscription.razorpay_order_id, "pay_wh_dup")
        second = svc.activate_from_webhook(db, subscription.razorpay_order_id, "pay_wh_dup")

        assert first.expires_at == second.expires_at

    def test_mark_failed_from_webhook_marks_a_pending_order_failed(self, db, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_order_response("order_wh_fail"))
        subscription = svc.create_order(db, PROFILE_ID, "1_month")

        svc.mark_failed_from_webhook(db, subscription.razorpay_order_id)

        db.refresh(subscription)
        assert subscription.status == "failed"

    def test_mark_failed_from_webhook_never_downgrades_an_already_paid_subscription(self, db, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_order_response("order_wh_late"))
        subscription = svc.create_order(db, PROFILE_ID, "1_month")
        svc.activate_from_webhook(db, subscription.razorpay_order_id, "pay_wh_late")

        # A late/out-of-order payment.failed for the same order, arriving after capture.
        svc.mark_failed_from_webhook(db, subscription.razorpay_order_id)

        db.refresh(subscription)
        assert subscription.status == "paid"
        assert svc.get_status(db, PROFILE_ID)["tier"] == "premium"

    def test_mark_failed_from_webhook_for_unknown_order_does_nothing(self, db):
        svc.mark_failed_from_webhook(db, "order_does_not_exist")  # must not raise
