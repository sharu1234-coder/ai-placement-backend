import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Subscription(Base):
    """One row per Razorpay order this profile has ever created -- never mutated into a
    different order, only ever transitioned created -> paid/failed by verify_payment(). This
    is deliberately an append-only order history (like ResumeAtsCheck/JobPreparationPlan),
    not a single mutable "current subscription" row: keeping every attempt (including failed
    and abandoned ones) is what makes the payment audit trail honest, and lets a renewal
    stack its duration onto whatever time is still remaining rather than guessing.

    amount_paise/currency/plan are snapshotted at order-creation time from the server-side
    PLAN_CATALOG (see subscription_service.py) -- the client never supplies a price, and this
    snapshot is what's actually charged regardless of any later catalog change.

    razorpay_order_id and razorpay_payment_id both carry a unique constraint: the former
    because Razorpay itself guarantees order ids are unique and this is our own idempotency
    key for /checkout; the latter because a single real payment must never be able to satisfy
    verification for more than one order row (replay-across-orders protection)."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)

    plan: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    razorpay_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)

    # created (order placed, no payment yet) -> paid (signature verified) or failed (bad
    # signature / payment never completed). There is no separate "cancelled" state: an order
    # the student simply abandons at checkout just stays "created" forever, which read paths
    # already treat as "not active" -- no extra state needed to represent it.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created", index=True)

    starts_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True, index=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.datetime.utcnow,
    )
    verified_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
