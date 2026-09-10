"""The other limit: how many times we may call the provider today.

Kept deliberately separate from app/services/job_hunt/entitlement.py, because the two answer
different questions and must never be allowed to answer each other's.

  entitlement    -- "has this student bought the right to search again?"   402, fixed by paying
  provider_budget -- "can this deployment's API key take another call?"     429, not fixed by paying

Jobvetta grants 50 requests per API key per UTC day, shared across every student on the
deployment. That ceiling belongs to the key, not to the person, so a premium student's
unlimited Job Hunt is unlimited in the only sense the product can honestly promise: we will
never refuse them for not having paid. We may still have to tell them the day's provider budget
is gone, and when that happens the message says so rather than implying they should buy
something.

The count lives in a table because the only deployment shape that matters has more than one
worker: a per-process counter lets each of two workers count to fifty and asks the key for a
hundred. The increment is a conditional UPDATE, so the database -- not application code --
decides whether a slot was available, and two workers racing for the last one produce one
winner.

Providers that do not publish a per-day ceiling (JobProvider.daily_call_budget is None) skip
all of this entirely: no row, no write, no cost. Metering a limit nobody imposed would be a
made-up restriction, and this module does not invent limits any more than the rest of Job Hunt
invents jobs.
"""

import datetime
import logging
from typing import Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.provider_call_budget import ProviderCallBudget
from app.services.job_hunt.providers.base import JobProviderRateLimitError

logger = logging.getLogger(__name__)


def _today() -> datetime.date:
    """The provider's own boundary. Jobvetta documents its quota per UTC day, and a local-time
    boundary would hand out a second allowance at the wrong hour and run the key dry early on
    whichever day the two disagreed."""
    return datetime.datetime.utcnow().date()


def reserve_call(
    db: Session,
    provider_name: str,
    daily_budget: Optional[int],
    *,
    today: Optional[datetime.date] = None,
) -> None:
    """Take one call out of today's budget, or raise JobProviderRateLimitError.

    Called immediately before the outbound request, never after. That is a deliberate bias: a
    request that dies in the socket is still counted, so this can slightly *under*-use the key.
    The alternative bias -- counting only what came back -- over-uses it, and over-using a key
    is a terms violation while under-using it is a slightly shorter day.

    JobProviderRateLimitError rather than a new exception type, so the API layer maps this the
    same way it already maps the provider answering 429 itself. From the student's side the two
    are the same fact ("the job service is out of capacity right now"), and inventing a second
    status code for the same fact would only give the client something new to mishandle.
    """
    if daily_budget is None:
        return

    day = today or _today()

    if daily_budget < 1:
        # The insert path below writes a row with call_count=1, which would quietly grant one
        # call against a budget of zero. Not a state any real provider is in, but "no calls
        # allowed" must not arithmetic its way into "one call allowed".
        logger.warning("Provider %s has a daily budget of %d; refusing", provider_name, daily_budget)
        raise JobProviderRateLimitError(
            f"{provider_name} daily call budget of {daily_budget} is exhausted for {day} (UTC)"
        )

    # Fast path: the row for today already exists and has room.
    if _take_slot(db, provider_name, day, daily_budget):
        return

    # No row yet, or no room. Try to create today's row; a concurrent request may beat us to
    # it, which is what the IntegrityError below means.
    try:
        with db.begin_nested():
            db.add(ProviderCallBudget(provider=provider_name, usage_date=day, call_count=1))
            db.flush()
    except IntegrityError:
        # Someone else created it between our UPDATE and our INSERT. Retry the UPDATE against
        # the row that now exists -- if it still has no room, the budget really is spent.
        if _take_slot(db, provider_name, day, daily_budget):
            return
    else:
        db.commit()
        return

    logger.warning(
        "Daily provider call budget exhausted for %s (%d calls, UTC %s)",
        provider_name,
        daily_budget,
        day,
    )
    raise JobProviderRateLimitError(
        f"{provider_name} daily call budget of {daily_budget} is exhausted for {day} (UTC)"
    )


def _take_slot(db: Session, provider_name: str, day: datetime.date, daily_budget: int) -> bool:
    """One atomic "increment if there is room". True when this call got a slot.

    The `call_count < daily_budget` predicate is what makes this safe under concurrency: the
    read and the write are the same statement, so two workers competing for the fiftieth call
    cannot both see forty-nine.
    """
    result = db.execute(
        update(ProviderCallBudget)
        .where(
            ProviderCallBudget.provider == provider_name,
            ProviderCallBudget.usage_date == day,
            ProviderCallBudget.call_count < daily_budget,
        )
        .values(
            call_count=ProviderCallBudget.call_count + 1,
            updated_at=datetime.datetime.utcnow(),
        )
    )
    if result.rowcount:
        db.commit()
        return True
    db.rollback()
    return False


def calls_used_today(
    db: Session, provider_name: str, *, today: Optional[datetime.date] = None
) -> int:
    """How much of today's budget is gone. For operators and tests; nothing in the request
    path reads this, because reading it and then acting on it would reintroduce exactly the
    check-then-act race reserve_call exists to avoid."""
    row = (
        db.query(ProviderCallBudget)
        .populate_existing()
        .filter(
            ProviderCallBudget.provider == provider_name,
            ProviderCallBudget.usage_date == (today or _today()),
        )
        .first()
    )
    return row.call_count if row is not None else 0
