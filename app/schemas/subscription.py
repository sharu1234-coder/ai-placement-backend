from pydantic import BaseModel


class SubscriptionStatusRead(BaseModel):
    """Static for now -- no billing integration exists yet. tier is always "free" and
    status is always "coming_soon" until real subscriptions are built; this endpoint exists
    so the Subscriptions screen shows a real (if currently unchanging) status rather than a
    hardcoded client-side string."""

    tier: str
    status: str
