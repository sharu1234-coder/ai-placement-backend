"""How many calls we have already made to a job provider today.

This table exists because two limits in this product are unrelated and were being conflated.

A premium student has unlimited *Job Hunt*. Jobvetta gives this deployment 50 *HTTP requests
per API key per UTC day*, shared by every student on it. "Unlimited" is a statement about what
the student bought; 50/day is a statement about what the key can physically do. Nothing in the
subscription tier makes the 51st call succeed, and a product that let a paying student believe
otherwise would be selling something it cannot deliver.

So the allowance is enforced in app/services/job_hunt/entitlement.py and the provider budget is
enforced here, and they produce different answers to the student: "your free search is used"
(402, and buying premium fixes it) versus "too many searches right now" (429, and buying
premium does not).

Why a table rather than a counter in memory. A process-local counter is wrong in the only
deployment shape that matters: with two workers, each one counts to 50 and the key gets asked
for 100. It also resets on every restart, so a crash loop is indistinguishable from a fresh
day. The row is the shared truth, and the increment is a conditional UPDATE that either takes
a slot or reports the budget spent -- the same "let the database arbitrate" approach the free
allowance uses.

One deliberate imprecision, stated rather than hidden: the count is incremented *before* the
request goes out, so a call that dies in the socket still counts. That errs toward
under-using the key rather than over-using it, which is the correct direction when the
consequence of the other mistake is a terms violation.
"""

import datetime

from sqlalchemy import Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ProviderCallBudget(Base):
    __tablename__ = "provider_call_budgets"

    id: Mapped[int] = mapped_column(primary_key=True)

    # JobProvider.name -- "jobvetta", "adzuna", ... One budget per provider, because the
    # limit belongs to that provider's key and switching providers must not inherit a count.
    provider: Mapped[str] = mapped_column(String(30), nullable=False)

    # UTC, because that is the boundary Jobvetta documents. A local-time boundary would give
    # a second allowance at the wrong moment and exhaust the key early on the day it shifted.
    usage_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("provider", "usage_date", name="uq_provider_call_budget_day"),
    )
