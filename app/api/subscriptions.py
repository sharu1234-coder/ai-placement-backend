from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.subscription import (
    CheckoutRequest,
    CheckoutResponse,
    PlanRead,
    SubscriptionStatusRead,
    VerifyPaymentRequest,
)
from app.services import subscription_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("/plans", response_model=List[PlanRead])
def get_plans() -> List[PlanRead]:
    """The server-authoritative plan catalog -- the ONLY place prices are defined. The
    Subscriptions screen renders this response directly rather than a hardcoded price list."""
    return [
        PlanRead(plan=key, label=plan["label"], amount_paise=plan["amount_paise"], duration_days=plan["duration_days"])
        for key, plan in subscription_service.PLAN_CATALOG.items()
    ]


@router.get("/status", response_model=SubscriptionStatusRead)
def get_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionStatusRead:
    profile = get_or_create_profile(db, current_user)
    return SubscriptionStatusRead(**subscription_service.get_status(db, profile.id))


@router.post("/checkout", response_model=CheckoutResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
def create_checkout(
    request: Request,
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutResponse:
    """Creates a Razorpay TEST MODE order for the given plan. The amount charged comes ONLY
    from the server-side PLAN_CATALOG (see subscription_service.py) keyed by `payload.plan`
    -- there is no field on this request the client could use to influence the price."""
    profile = get_or_create_profile(db, current_user)
    try:
        subscription = subscription_service.create_order(db, profile.id, payload.plan)
    except subscription_service.UnknownPlanError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except subscription_service.RazorpayNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except subscription_service.RazorpayApiError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    return CheckoutResponse(
        razorpay_order_id=subscription.razorpay_order_id,
        razorpay_key_id=settings.razorpay_key_id or "",
        amount_paise=subscription.amount_paise,
        currency=subscription.currency,
        plan=subscription.plan,
    )


@router.post("/verify", response_model=SubscriptionStatusRead)
@limiter.limit("20/hour")
def verify_payment(
    request: Request,
    payload: VerifyPaymentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionStatusRead:
    """Activates premium ONLY once the Razorpay signature is verified server-side against the
    order this exact profile created. Safe to call more than once for the same order (a
    retried/duplicate callback is a no-op success, not a re-processing or an error)."""
    profile = get_or_create_profile(db, current_user)
    try:
        subscription_service.verify_payment(
            db,
            profile.id,
            payload.razorpay_order_id,
            payload.razorpay_payment_id,
            payload.razorpay_signature,
        )
    except subscription_service.OrderNotFoundError as exc:
        # 404, not 403 -- never confirm/deny another profile's order exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except subscription_service.RazorpayNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except subscription_service.SignatureVerificationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except subscription_service.DuplicatePaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    return SubscriptionStatusRead(**subscription_service.get_status(db, profile.id))
