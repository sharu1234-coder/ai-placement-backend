"""A bounded, in-process TTL cache for world retrievals.

In-process and not a table, deliberately. VA-5's brief is explicit that a transient retrieval
must not create a new data-retention surface, and a cache table would be exactly that: external
job adverts, persisted, with a schema migration and a retention question attached. The existing
`JobListing` table already persists adverts for the authenticated job-search feature, where a
student's saved jobs genuinely need to outlive a request; world retrieval has no such need.

What that costs, stated rather than glossed: the cache is per worker, so N workers can each
spend one provider call for the same query. That is why it is **not** the mechanism that
protects the provider's quota -- `app/services/job_hunt/provider_budget.py` does that, from the
database, across workers. This cache exists to keep one student's repeated question from
costing repeated calls, and nothing more.

TTL is the provider's `cache_ttl`, which each adapter sets from its own terms, not from
performance preference.
"""

from __future__ import annotations

import datetime
import threading
from collections import OrderedDict
from typing import Any, Generic, Optional, TypeVar

T = TypeVar("T")

#: Entries kept before the oldest is evicted. Bounded because an unbounded dict keyed by a
#: user-supplied query string is a memory-growth path a caller controls.
DEFAULT_MAX_ENTRIES = 256


class TtlCache(Generic[T]):
    """Least-recently-used, with a per-entry expiry.

    Lock-guarded: the sync routes run in Starlette's threadpool, so two threads reach this
    concurrently in normal operation.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries <= 0:
            raise ValueError("a cache needs room for at least one entry")
        self._max_entries = max_entries
        self._entries: "OrderedDict[str, tuple[datetime.datetime, T]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: str, *, now: Optional[datetime.datetime] = None) -> Optional[T]:
        reference = now or _utcnow()
        with self._lock:
            found = self._entries.get(key)
            if found is None:
                self.misses += 1
                return None
            expires_at, value = found
            if expires_at <= reference:
                # Dropped on read rather than by a sweeper: an expired entry that is never
                # read again costs nothing, and a background thread to collect it would be
                # more machinery than the problem.
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: T, ttl: datetime.timedelta, *, now: Optional[datetime.datetime] = None) -> None:
        if ttl <= datetime.timedelta(0):
            # A non-positive TTL means "do not reuse this". Storing it would make the next read
            # a hit-then-expire, which is the same outcome with extra steps.
            return
        reference = now or _utcnow()
        with self._lock:
            self._entries[key] = (reference + ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def query_key(*parts: Any) -> str:
    """A cache key from query parts.

    Lower-cased and `|`-joined with `None` spelled out, so that a key cannot collide by
    accident: without an explicit marker, ("python", None) and (None, "python") both render as
    "python" and one query answers the other -- which, for a geography parameter, means
    answering a Pune question with Bangalore data.
    """
    return "|".join("~none~" if part is None else str(part).strip().lower() for part in parts)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
