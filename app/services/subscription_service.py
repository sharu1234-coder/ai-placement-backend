"""Razorpay TEST MODE order creation + payment verification, and the resulting premium
entitlement. Uses Razorpay's Orders API (one-time payment) rather than its Subscriptions API
(recurring auto-charge) -- these are fixed-duration purchases the student re-buys manually
each time, not an auto-renewing mandate, so Orders is the correct fit, not a simplification.
See https://razorpay.com/docs/api/orders/ and the standard checkout verification flow at
https://razorpay.com/docs/payments/server-integration/php/payment-gateway/build-integration/.

Pricing lives ONLY here, server-side -- the client sends a plan key, never an amount, and
/checkout always re-reads the price from PLAN_CATALOG. This is what makes client-side price
manipulation impossible: there is no code path that accepts an amount from the request body.
"""

import datetime
import hashlib
import hmac
import logging
from typing import Optional

import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.subscription import Subscription

logger = logging.getLogger(__name__)

RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"

# amount_paise is in the smallest currency subunit Razorpay expects (paise, i.e. INR * 100)
# -- see the Orders API "amount" field. duration_days is the plan length actually granted on
# successful verification; deliberately NOT calendar months (28-31 day ambiguity) so renewal
# math (see verify_payment) is unambiguous.
PLAN_CATALOG: dict[str, dict] = {
    "1_month": {"label": "1 Month", "amount_paise": 19_900, "duration_days": 30},
    "3_months": {"label": "3 Months", "amount_paise": 49_900, "duration_days": 90},
    "6_months": {"label": "6 Months", "amount_paise": 79_900, "duration_days": 180},
    "1_year": {"label": "1 Year", "amount_paise": 99_900, "duration_days": 365},
}


class SubscriptionError(Exception):
    """Base for every error this module raises -- callers translate these to HTTP responses
    without needing to know which specific subclass they caught."""


class UnknownPlanError(SubscriptionError):
    pass


class RazorpayNotConfiguredError(SubscriptionError):
    pass


class RazorpayApiError(SubscriptionError):
    pass


class OrderNotFoundError(SubscriptionError):
    """Also raised (not a separate error) when the order exists but belongs to a different
    profile -- returning 404 either way avoids leaking which order ids exist for other
    users, same ownership pattern used everywhere else in this codebase."""


class SignatureVerificationError(SubscriptionError):
    pass


class DuplicatePaymentError(SubscriptionError):
    """A razorpay_payment_id that's already attached to a different order -- the DB's own
    unique constraint is the real guarantee; this just turns that IntegrityError into a
    clean, expected error instead of a 500."""


def create_order(db: Session, profile_id: int, plan: str) -> Subscription:
    plan_def = PLAN_CATALOG.get(plan)
    if plan_def is None:
        raise UnknownPlanError(f"Unknown plan '{plan}'. Valid plans: {', '.join(PLAN_CATALOG)}.")
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise RazorpayNotConfiguredError(
            "Razorpay is not configured -- set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET "
            "(use TEST MODE keys, rzp_test_...) before subscriptions can be purchased."
        )

    # Receipt must be <=40 chars and unique per Razorpay's own constraint; profile_id + a
    # microsecond timestamp is enough entropy without needing a DB round-trip first.
    receipt = f"sub_{profile_id}_{int(datetime.datetime.utcnow().timestamp() * 1000)}"[:40]
    try:
        response = httpx.post(
            RAZORPAY_ORDERS_URL,
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
            json={"amount": plan_def["amount_paise"], "currency": "INR", "receipt": receipt},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        logger.error("Could not reach Razorpay to create an order: %s", exc)
        raise RazorpayApiError("Could not reach the payment provider. Please try again.") from exc
    if response.status_code != 200:
        # response.text may echo back request details or provider-internal error shapes --
        # logged in full server-side for debugging, but never handed to the API client, which
        # could end up rendered in an Android Toast/log.
        logger.error("Razorpay order creation failed (%s): %s", response.status_code, response.text)
        raise RazorpayApiError("The payment provider rejected the order request. Please try again.")

    order = response.json()
    subscription = Subscription(
        profile_id=profile_id,
        plan=plan,
        amount_paise=plan_def["amount_paise"],
        currency="INR",
        razorpay_order_id=order["id"],
        status="created",
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def _get_active_subscription(db: Session, profile_id: int, now: datetime.datetime) -> Optional[Subscription]:
    return (
        db.query(Subscription)
        # See the matching comment in verify_payment -- guards the same identity-map
        # staleness risk for whichever row this resolves to.
        .populate_existing()
        .filter(
            Subscription.profile_id == profile_id,
            Subscription.status == "paid",
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc())
        .first()
    )


def verify_payment(
    db: Session,
    profile_id: int,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> Subscription:
    """Activates premium ONLY after the HMAC signature checks out against this exact
    order_id + payment_id pair. Ownership is enforced by scoping the lookup to profile_id --
    a token for profile B can never verify (or even discover the existence of) profile A's
    order. Idempotent: a duplicate callback/retry for an order already marked "paid" returns
    the existing row unchanged rather than re-extending expiry or erroring."""
    if not settings.razorpay_key_secret:
        # Configured at order-creation time but unset by the time verification runs (a
        # mid-flight config change/rollback) -- fail closed with a clear error rather than
        # crashing on .encode() below with an unhandled AttributeError.
        raise RazorpayNotConfiguredError("Razorpay is not configured -- cannot verify payments right now.")

    subscription = (
        db.query(Subscription)
        # populate_existing(): without it, a row already present in this session's identity
        # map (e.g. this same object loaded earlier by create_order, or by an earlier call in
        # a long-lived session) is returned as-is from the identity map, NOT refreshed from
        # this query's actual result row -- so a concurrent request's commit (another session
        # flipping this exact row to "paid" in between) would be invisible here, and every
        # idempotency/staleness check below would be working off stale in-memory data.
        .populate_existing()
        .filter(Subscription.razorpay_order_id == razorpay_order_id, Subscription.profile_id == profile_id)
        .first()
    )
    if subscription is None:
        raise OrderNotFoundError("No matching order found for this account.")

    if subscription.status == "paid":
        return subscription

    expected_signature = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, razorpay_signature):
        subscription.status = "failed"
        db.commit()
        raise SignatureVerificationError("Payment signature could not be verified.")

    return _activate_paid(db, subscription, razorpay_payment_id)


def _activate_paid(db: Session, subscription: Subscription, razorpay_payment_id: str) -> Subscription:
    """Shared by verify_payment (checkout-callback path) and activate_from_webhook (webhook
    path) -- both reach here only after establishing, by their own separate means, that this
    exact payment is authentically Razorpay's. Renewing before the current subscription
    lapses stacks the new duration on top of the remaining time rather than discarding it.

    Atomic conditional UPDATE (not "load, mutate, commit"): closes the race where two
    concurrent activations for the same order (a genuine duplicate callback/webhook redelivery
    arriving twice at once, not just sequentially) both pass the "not yet paid" check above
    before either commits. Only the request whose UPDATE actually flips a "created"/"failed"
    row to "paid" proceeds to treat this as a fresh activation; the loser re-fetches and
    returns the winner's already-committed row, exactly like the sequential idempotent-return
    path above."""
    now = datetime.datetime.utcnow()
    plan_def = PLAN_CATALOG[subscription.plan]
    current_active = _get_active_subscription(db, subscription.profile_id, now)
    base = current_active.expires_at if current_active else now
    new_expires_at = base + datetime.timedelta(days=plan_def["duration_days"])

    try:
        db.execute(
            update(Subscription)
            .where(Subscription.id == subscription.id, Subscription.status != "paid")
            .values(
                razorpay_payment_id=razorpay_payment_id,
                status="paid",
                starts_at=now,
                expires_at=new_expires_at,
                verified_at=now,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DuplicatePaymentError(
            "This payment has already been used to verify a different order."
        ) from exc

    # Whether THIS request's UPDATE actually matched a row or lost the race to a concurrent
    # duplicate call (rowcount 0, because the other request already flipped status to "paid"
    # first), refreshing and returning the row is correct either way: it reflects whichever
    # request's write actually landed, for the same order/payment/plan either request would
    # have computed near-identically.
    db.refresh(subscription)
    return subscription


def verify_webhook_signature(raw_body: bytes, signature: str, webhook_secret: str) -> bool:
    """X-Razorpay-Signature is HMAC-SHA256(raw request body, webhook secret), hex digest --
    see https://razorpay.com/docs/webhooks/validate-test/. This is a SEPARATE secret from
    RAZORPAY_KEY_SECRET, configured independently in the Razorpay dashboard's Webhooks
    section. The signature MUST be computed over the exact raw bytes Razorpay sent -- a
    re-serialized/re-parsed JSON body will not produce a matching signature even with
    identical field values (key ordering/whitespace differ), which is why the caller must
    pass `await request.body()` untouched, never a re-dumped dict."""
    expected = hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def activate_from_webhook(db: Session, razorpay_order_id: str, razorpay_payment_id: str) -> Optional[Subscription]:
    """Called only after the caller (the /webhook route) has already verified
    X-Razorpay-Signature over the raw body -- this function does no signature checking of its
    own; it trusts that the request's authenticity was already established. Unlike
    verify_payment (scoped to one profile via the caller's JWT), a webhook carries no user
    identity at all -- razorpay_order_id is the only lookup key, which is safe ONLY because
    the signature check already proves this payload really came from Razorpay, not an
    attacker guessing order ids. Returns None (not an error) for an order id this backend
    has no record of -- a webhook for a stale/foreign/test-mode-mismatched order should never
    5xx and trigger Razorpay's retry storm; it's simply logged and ignored."""
    subscription = (
        db.query(Subscription).populate_existing().filter(Subscription.razorpay_order_id == razorpay_order_id).first()
    )
    if subscription is None:
        logger.warning("Razorpay webhook for unknown order_id=%s -- no matching subscription.", razorpay_order_id)
        return None
    if subscription.status == "paid":
        return subscription
    return _activate_paid(db, subscription, razorpay_payment_id)


def mark_failed_from_webhook(db: Session, razorpay_order_id: str) -> None:
    """A payment.failed webhook. Never downgrades a subscription that's already "paid" --
    webhook delivery order isn't guaranteed, so a late/out-of-order failure event for a
    payment that a subsequent capture already activated must not revoke real, already-granted
    premium access."""
    subscription = (
        db.query(Subscription).populate_existing().filter(Subscription.razorpay_order_id == razorpay_order_id).first()
    )
    if subscription is None or subscription.status == "paid":
        return
    subscription.status = "failed"
    db.commit()


def is_premium(db: Session, profile_id: int) -> bool:
    """Single source of truth for server-side premium enforcement (app.api.deps.require_premium)
    -- same "an active, unexpired, paid subscription row" definition get_status already uses,
    so a route's enforcement can never drift from what the client's own status check reports."""
    return _get_active_subscription(db, profile_id, datetime.datetime.utcnow()) is not None


def get_status(db: Session, profile_id: int) -> dict:
    now = datetime.datetime.utcnow()
    active = _get_active_subscription(db, profile_id, now)
    if active is not None:
        return {
            "tier": "premium",
            "status": "active",
            "plan": active.plan,
            "expires_at": active.expires_at,
        }

    had_any_paid = (
        db.query(Subscription)
        .filter(Subscription.profile_id == profile_id, Subscription.status == "paid")
        .first()
    )
    if had_any_paid is not None:
        return {"tier": "free", "status": "expired", "plan": None, "expires_at": None}

    return {"tier": "free", "status": "never_subscribed", "plan": None, "expires_at": None}
