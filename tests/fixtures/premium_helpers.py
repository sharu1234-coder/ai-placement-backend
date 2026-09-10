"""Give a test account a real, active subscription.

Deliberately a real `subscriptions` row rather than a monkeypatched `is_premium`. Job Hunt's
entitlement ledger, its alert runner and its advanced-filter gate each import that function
into their own module namespace, so patching it means patching it in three places and getting
one of them wrong is a test that passes for the wrong reason. More importantly, a patched
predicate proves nothing about the thing the product actually depends on: that
`subscription_service.is_premium` returns True for a paid, unexpired row and False once it
lapses. Writing the row exercises that for real.

`expire_premium` exists for the same reason in reverse -- a lapse in these tests is a real
expiry date moving into the past, which is exactly what happens in production, rather than a
predicate being flipped.
"""

import datetime

from sqlalchemy.orm import Session

from app.models.subscription import Subscription
from app.models.user import User
from app.services.profile_service import get_or_create_profile


def grant_premium(db: Session, email: str, *, days: int = 30) -> Subscription:
    """One paid, unexpired subscription for the account with this email.

    Mirrors what subscription_service._activate_paid writes on a verified payment: status
    "paid", a start in the past and an expiry in the future. The razorpay ids are unique
    per call because both columns carry a unique constraint, and a test that granted premium
    to two accounts would otherwise fail on the second for a reason that has nothing to do
    with what it was checking.
    """
    now = datetime.datetime.utcnow()
    user = db.query(User).filter(User.email == email).one()
    profile = get_or_create_profile(db, user)
    subscription = Subscription(
        profile_id=profile.id,
        plan="1_month",
        amount_paise=19_900,
        currency="INR",
        razorpay_order_id=f"order_test_{profile.id}_{int(now.timestamp() * 1_000_000)}",
        razorpay_payment_id=f"pay_test_{profile.id}_{int(now.timestamp() * 1_000_000)}",
        status="paid",
        starts_at=now - datetime.timedelta(days=1),
        expires_at=now + datetime.timedelta(days=days),
        verified_at=now - datetime.timedelta(days=1),
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def expire_premium(db: Session, email: str) -> None:
    """Move every paid subscription on this account into the past.

    Not a delete: the product's rule is that history survives a lapse, and a test that erased
    the row would be checking a state production never reaches.
    """
    now = datetime.datetime.utcnow()
    user = db.query(User).filter(User.email == email).one()
    profile = get_or_create_profile(db, user)
    for subscription in (
        db.query(Subscription).filter(Subscription.profile_id == profile.id).all()
    ):
        subscription.expires_at = now - datetime.timedelta(days=1)
    db.commit()
