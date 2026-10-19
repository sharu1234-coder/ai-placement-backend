"""The freshness policy, per data category.

One universal cache duration would be wrong in both directions at once: a vacancy that closed
yesterday is actively harmful at six hours old, while a role's typical skill expectations do
not change between Tuesday and Thursday. So the thresholds are per category, and each one has
a reason rather than a round number.

These govern how a *retrieved* value is labelled. How long a value may be *reused* is the
cache's question (`app/world/cache.py`), and the two are deliberately different numbers: a
provider's terms may permit retention longer than the data stays useful, or shorter.
"""

from __future__ import annotations

import datetime
from typing import Optional

from app.world.contracts import DataCategory, Freshness

#: (fresh_until, aging_until) per category. Past the second value a result is STALE.
#:
#: * JOB_OPENING -- a vacancy can be filled the day after it is posted, and a student applying
#:   to a closed advert loses real time. Two days fresh, a week before it is stale.
#: * COMPANY_HIRING -- derived from adverts, so it ages with them, but a hiring posture is a
#:   slower-moving thing than one vacancy. The spec's own note that it "may become stale
#:   quickly" is why this is days rather than weeks.
#: * ROLE_REQUIREMENTS -- what a role asks for moves with the industry, not with one advert.
#: * SKILL_DEMAND -- a trend claim needs dated evidence; a month-old sample is still evidence,
#:   a quarter-old one is a different market.
#: * GEOGRAPHIC_COMPARISON -- two samples, so it is only as fresh as the staler one; the
#:   comparison itself is held to the same bar as the demand figures it compares.
#: * LEARNING_RESOURCE -- no approved source exists (see `app/world/capabilities.py`), so
#:   nothing is ever classified under it. The row is here so that adding a source later has to
#:   confront version-awareness rather than inherit a default.
_THRESHOLDS: dict[DataCategory, tuple[datetime.timedelta, datetime.timedelta]] = {
    DataCategory.JOB_OPENING: (datetime.timedelta(days=2), datetime.timedelta(days=7)),
    DataCategory.COMPANY_HIRING: (datetime.timedelta(days=7), datetime.timedelta(days=30)),
    DataCategory.ROLE_REQUIREMENTS: (datetime.timedelta(days=30), datetime.timedelta(days=180)),
    DataCategory.SKILL_DEMAND: (datetime.timedelta(days=30), datetime.timedelta(days=90)),
    DataCategory.GEOGRAPHIC_COMPARISON: (datetime.timedelta(days=30), datetime.timedelta(days=90)),
    DataCategory.LEARNING_RESOURCE: (datetime.timedelta(days=90), datetime.timedelta(days=365)),
}


def thresholds(category: DataCategory) -> tuple[datetime.timedelta, datetime.timedelta]:
    return _THRESHOLDS[category]


def classify(
    category: DataCategory,
    published_at: Optional[datetime.datetime],
    retrieved_at: datetime.datetime,
    *,
    now: Optional[datetime.datetime] = None,
) -> Freshness:
    """Label a value's freshness.

    Age is measured from `published_at` when the source supplies it, because that is the age of
    the *fact*. When it does not, the answer is `UNDATED` rather than a guess from
    `retrieved_at`: retrieval time measures our own behaviour, and using it would make a
    six-month-old advert look fresh because we fetched it a second ago. Calling it STALE
    instead would be inventing the bad case. Undated is the truth.
    """
    if published_at is None:
        return Freshness.UNDATED

    reference = now or _utcnow()
    age = reference - _aware(published_at)
    fresh_until, aging_until = thresholds(category)
    if age <= fresh_until:
        return Freshness.FRESH
    if age <= aging_until:
        return Freshness.AGING
    return Freshness.STALE


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: datetime.datetime) -> datetime.datetime:
    """Treat a naive timestamp as UTC.

    Providers vary: some send an offset, some do not. Subtracting a naive from an aware
    datetime raises, and the failure would surface as a provider outage rather than as the
    timezone bug it is -- so the assumption is made here, once, and written down.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)
