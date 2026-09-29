"""One bounded budget per logical AI operation -- the single owner of application retry policy.

F12-06: this module used to be a *per-call* retry (1 attempt + 2 retries), which is a different
thing from a budget. Nothing owned the operation, so three independent multipliers composed with
nothing bounding their product:

    schema-correction stages (2)  x  app attempts (3)  x  anthropic SDK attempts (3)  =  18

Measured on the wire, not assumed: the reachable worst case was 6 provider calls (Ollama, 24
minutes at the 240s per-attempt timeout) and 16 provider calls (Anthropic, 32 minutes at 120s).
A sustained 429 on the Anthropic path made 9 calls and slept 180.6s, because the SDK honoured
`Retry-After: 30` six times while this layer retried on top of it. Phase 17's own logs contain
the Ollama case twice: 394924ms = 240000 + 200 + 154724 and 617078ms = 240000 + 200 + 240000 +
400 + 136478. No layer held a deadline, so the product of the attempt counts times the
per-attempt timeout *was* the operation's duration, and the Android client's 300s read timeout
was the only thing that ever stopped it -- the client enforcing the server's AI budget.

So the shape here is an operation, not a call. One AIOperationBudget is created per logical AI
operation and is shared by every stage of it (the initial prompt, the one schema correction, and
any transient retry), which is what makes the total deterministic:

    logical AI operation  (<= MAX_PROVIDER_CALLS provider calls, <= total_seconds wall clock)
      |- stage "initial"      : 1 attempt + at most 1 bounded transient retry
      \\- stage "correction"  : 1 attempt + at most 1 bounded transient retry
                               ... but both stages draw down the SAME call cap and the SAME
                                   deadline, so the correction cannot multiply the budget.

The deadline is enforced by two rules and no new magic numbers:

  1. every attempt's timeout is clamped to the remaining budget, so no single attempt can run
     past the deadline;
  2. a retry starts only if the full per-attempt timeout still fits in what remains -- a retry
     that cannot be given a fair chance is not worth a provider call.

Rule 2 is what makes this correct rather than merely smaller, and it needs no special-casing of
failure kinds, because the budget discriminates by itself: a connection refused at 0.4s leaves
299.6s and is retried (which is where retries genuinely pay), while a read timeout at 240s
leaves 60s and is not (which is exactly the stacking that produced the 617s case).

What this does and does not guarantee, stated precisely because the difference showed up in
live validation rather than in theory:

  * GUARANTEED, unconditionally: at most MAX_PROVIDER_CALLS provider calls per logical
    operation, and no call started after the deadline has passed. These are decisions this
    code makes, so nothing can take them away.
  * BEST EFFORT: the wall clock. total_seconds bounds the provider work this layer will
    *commit to*, not the elapsed time of the operation. Observed live on a dev machine running
    several 8B-parameter inferences at once: an operation logged ms=753141 having made one
    1.3s provider call, because a 20s Retry-After sleep overshot by minutes of CPU starvation;
    and a call given timeout=240.0s returned ok after 880s, because httpx's timeouts are
    per-socket-operation and a thread that is never scheduled cannot raise one either. A
    starved process cannot measure or enforce its own wall clock, and no in-process budget can
    fix that. What the budget still did in both cases was refuse the *next* provider call.

So the number to trust, and the one the tests pin down, is the call count.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, NoReturn, Optional, TypeVar

from app.ai.provider import AIGenerationError

logger = logging.getLogger(__name__)

R = TypeVar("R")

# 1 initial attempt + 1 bounded retry per stage. Was 3; the measured evidence showed the third
# attempt only ever bought latency -- every Phase 17 case that eventually succeeded did so on a
# call that a 2-attempt policy would also have made, or timed out on one that cost 240s.
MAX_ATTEMPTS_PER_STAGE = 2

# The hard cap on provider calls for one logical operation, across ALL of its stages. Three
# covers every realistic combination: a transient blip (2), a schema failure (2), or a schema
# failure whose correction also hits a blip (3). A fourth call means both a schema failure AND
# two transient failures have already happened -- fall back to the deterministic template
# instead of paying for a fourth round trip.
MAX_PROVIDER_CALLS = 3

# Fixed, not exponential: bounded and deterministic, so a test can assert the exact total wall
# clock of a retrying operation instead of reproducing a growth curve.
RETRY_BACKOFF_SECONDS = 0.5


class FailureCategory(str, Enum):
    """Every way a provider call can fail, split by whether repeating it could possibly help."""

    # Retryable: the identical request can plausibly succeed on a second attempt.
    TRANSIENT_NETWORK = "transient_network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    # Not retryable: an unchanged request cannot fix an unchanged condition, so a retry only
    # adds latency and burns the caller's budget.
    AUTH = "auth"
    CLIENT_ERROR = "client_error"
    SCHEMA = "schema"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


RETRYABLE_CATEGORIES = frozenset(
    {
        FailureCategory.TRANSIENT_NETWORK,
        FailureCategory.TIMEOUT,
        FailureCategory.RATE_LIMIT,
        FailureCategory.SERVER_ERROR,
    }
)

NON_RETRYABLE_CATEGORIES = frozenset(FailureCategory) - RETRYABLE_CATEGORIES

# asyncio.CancelledError derives from BaseException (3.8+), so `except Exception` already misses
# it -- this tuple exists so the invariant is explicit and testable rather than incidental, and
# so a future widening of the handler below cannot silently make cancellation retryable.
# concurrent.futures.CancelledError is an alias of asyncio.CancelledError on 3.8+.
CANCELLATION = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


@dataclass(frozen=True)
class FailureVerdict:
    """A provider's classification of one failure. `retry_after_seconds` carries a server's own
    `Retry-After` when it sent one: the anthropic SDK's retry loop is now disabled (max_retries=0)
    so that this layer is authoritative, which means this layer has to honour the header the SDK
    used to honour, or correct rate-limit behaviour would regress."""

    category: FailureCategory
    retry_after_seconds: Optional[float] = None

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES


class AIBudgetExhausted(AIGenerationError):
    """The operation ran out of calls or wall clock before a stage could even be attempted.

    Deliberately a subclass of AIGenerationError: all 19 AI call sites already fall back to a
    deterministic template on AIGenerationError, so budget exhaustion reaches exactly the same
    no-AI fallback with no service-layer change and no new failure mode to handle."""


class AIOperationBudget:
    """One logical AI operation's single retry and time budget.

    Use as a context manager, once per logical operation, so the operation summary is logged
    whichever way it ends:

        with AIOperationBudget(operation="ParsedResume", provider="ollama",
                               per_attempt_seconds=240) as budget:
            raw = budget.call(lambda timeout: post(..., timeout=timeout),
                              classify=_classify, stage="initial")
    """

    def __init__(
        self,
        *,
        operation: str,
        provider: str,
        per_attempt_seconds: float,
        total_seconds: Optional[float] = None,
    ) -> None:
        if total_seconds is None:
            from app.core.config import settings

            total_seconds = settings.ai_total_budget_seconds
        self.operation = operation
        self.provider = provider
        self.per_attempt_seconds = float(per_attempt_seconds)
        self.total_seconds = float(total_seconds)
        # Short, random, and not persisted: enough to group one operation's attempt lines in a
        # log without a new table or any user-identifying value.
        self.id = uuid.uuid4().hex[:8]
        self.calls = 0
        self._started = time.monotonic()
        self._last_category: Optional[FailureCategory] = None

    # -- the clock ---------------------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed_seconds)

    # -- the context manager -----------------------------------------------------------

    def __enter__(self) -> "AIOperationBudget":
        # Start the clock at entry, not construction, so a budget built slightly ahead of use
        # does not silently donate part of its own deadline.
        self._started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            outcome = "ok"
        elif issubclass(exc_type, CANCELLATION):
            outcome = f"failed:{FailureCategory.CANCELLED.value}"
        else:
            category = self._last_category.value if self._last_category else type(exc).__name__
            outcome = f"failed:{category}"
        logger.info(
            "ai_operation op=%s id=%s provider=%s calls=%d/%d ms=%.0f budget_ms=%.0f outcome=%s",
            self.operation, self.id, self.provider, self.calls, MAX_PROVIDER_CALLS,
            self.elapsed_seconds * 1000, self.total_seconds * 1000, outcome,
        )
        return False

    def record(self, category: FailureCategory) -> None:
        """Name the category this operation ultimately failed on, for a failure the budget
        cannot see for itself. A schema validation failure is the real case: the provider calls
        all succeeded, so without this the operation line would end `failed:AIGenerationError`
        and lose the one detail worth knowing."""
        self._last_category = category

    # -- the one way to reach a provider -----------------------------------------------

    def call(
        self,
        fn: Callable[[float], R],
        *,
        classify: Callable[[Exception], FailureVerdict],
        stage: str,
    ) -> R:
        """Run one stage of this operation: `fn(timeout)`, plus at most one bounded retry for a
        genuinely transient failure that still fits inside the operation's remaining budget.

        `fn` receives the effective per-attempt timeout in seconds, already clamped to what the
        operation has left, and must apply it to the provider call -- that clamp is half of how
        the deadline is enforced.

        A non-retryable failure, and the last attempt's failure, are re-raised unchanged so each
        provider's own exception translation still produces its own accurate message. Only
        "could not even start an attempt" becomes AIBudgetExhausted."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS_PER_STAGE + 1):
            timeout = self._open_attempt(stage=stage, last_exc=last_exc)
            started = time.monotonic()
            try:
                result = fn(timeout)
            except CANCELLATION:
                # Never retried, and never reclassified: the caller has gone away, so another
                # provider call would be work nobody is waiting for.
                self._last_category = FailureCategory.CANCELLED
                self._log_attempt(stage, attempt, timeout, started,
                                  FailureCategory.CANCELLED.value, "no_retry=cancelled")
                raise
            except Exception as exc:
                verdict = classify(exc)
                self._last_category = verdict.category
                last_exc = exc
                if not verdict.retryable:
                    self._log_attempt(stage, attempt, timeout, started, verdict.category.value,
                                      "no_retry=not_retryable")
                    raise
                if attempt == MAX_ATTEMPTS_PER_STAGE:
                    self._log_attempt(stage, attempt, timeout, started, verdict.category.value,
                                      "no_retry=stage_attempts")
                    raise
                allowed, delay, refusal = self._may_retry(verdict)
                if not allowed:
                    self._log_attempt(stage, attempt, timeout, started, verdict.category.value,
                                      f"no_retry={refusal}")
                    raise
                self._log_attempt(stage, attempt, timeout, started, verdict.category.value,
                                  f"retry_in={delay:.1f}s")
                time.sleep(delay)
            else:
                self._log_attempt(stage, attempt, timeout, started, "ok", "")
                return result
        raise AssertionError("unreachable")  # pragma: no cover -- always returns or raises

    # -- budget enforcement ------------------------------------------------------------

    def _open_attempt(self, *, stage: str, last_exc: Optional[Exception]) -> float:
        """Charge one provider call to the budget and return its clamped timeout, or refuse."""
        if self.calls >= MAX_PROVIDER_CALLS:
            self._exhausted("call_cap", stage, last_exc)
        remaining = self.remaining_seconds
        if remaining <= 0:
            self._exhausted("deadline", stage, last_exc)
        self.calls += 1
        return min(self.per_attempt_seconds, remaining)

    def _may_retry(self, verdict: FailureVerdict) -> tuple[bool, float, str]:
        """Whether a retry fits in what is left, and how long to wait first."""
        if self.calls >= MAX_PROVIDER_CALLS:
            return False, 0.0, "call_cap"
        delay = (
            RETRY_BACKOFF_SECONDS
            if verdict.retry_after_seconds is None
            else max(0.0, verdict.retry_after_seconds)
        )
        # The retry must be able to run its full per-attempt timeout inside the remaining
        # budget, waiting time included. A clamped-to-almost-nothing retry would just burn a
        # provider call to time out again.
        if delay + self.per_attempt_seconds > self.remaining_seconds:
            return False, delay, "deadline"
        return True, delay, ""

    def _exhausted(self, reason: str, stage: str, last_exc: Optional[Exception]) -> NoReturn:
        logger.warning(
            "ai_budget_exhausted op=%s id=%s provider=%s stage=%s reason=%s calls=%d ms=%.0f",
            self.operation, self.id, self.provider, stage, reason, self.calls,
            self.elapsed_seconds * 1000,
        )
        raise AIBudgetExhausted(
            f"AI operation budget exhausted ({reason}) after {self.calls} provider call(s) "
            f"in {self.elapsed_seconds:.1f}s"
        ) from last_exc

    # -- observability -----------------------------------------------------------------

    def _log_attempt(
        self, stage: str, attempt: int, timeout: float, started: float, outcome: str, note: str
    ) -> None:
        """One line per provider call. Deliberately carries no prompt, no response text, no
        token counts, no API key and no user content -- only the shape of the retry graph."""
        log = logger.info if outcome == "ok" else logger.warning
        log(
            "ai_attempt op=%s id=%s provider=%s stage=%s call=%d/%d attempt=%d/%d "
            "timeout=%.1fs ms=%.0f outcome=%s%s",
            self.operation, self.id, self.provider, stage, self.calls, MAX_PROVIDER_CALLS,
            attempt, MAX_ATTEMPTS_PER_STAGE, timeout, (time.monotonic() - started) * 1000,
            outcome, f" {note}" if note else "",
        )
