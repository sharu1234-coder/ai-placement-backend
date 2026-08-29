import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class PlanRead(BaseModel):
    """One entry of the server-authoritative plan catalog -- the Subscriptions screen renders
    exactly this, never a client-side hardcoded price list, so pricing can never drift out of
    sync with what /checkout will actually charge."""

    plan: str
    label: str
    amount_paise: int
    duration_days: int


class SubscriptionStatusRead(BaseModel):
    """The one source of truth for whether this profile currently has premium. tier/status
    are server-derived from real Subscription rows -- never settable by the client, never
    cached client-side as the source of truth (Android always re-reads this rather than
    trusting a locally-stored flag)."""

    tier: Literal["free", "premium"]
    status: Literal["never_subscribed", "active", "expired"]
    plan: Optional[str] = None
    expires_at: Optional[datetime.datetime] = None


class CheckoutRequest(BaseModel):
    """Deliberately just a plan KEY -- there is no amount field anywhere on this request, so
    there is no code path by which a client could supply its own price."""

    plan: str


class CheckoutResponse(BaseModel):
    razorpay_order_id: str
    # The publishable key ID Razorpay's Android Checkout SDK needs client-side -- not the
    # secret, which never leaves the backend.
    razorpay_key_id: str
    amount_paise: int
    currency: str
    plan: str


class VerifyPaymentRequest(BaseModel):
    """The three fields Razorpay Checkout hands back to the client after a payment attempt.
    Verified server-side against the order this profile actually created; nothing here is
    trusted until verify_payment's HMAC check passes."""

    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
