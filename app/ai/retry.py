"""Small, bounded retry-with-backoff for genuinely transient AI provider failures -- a
network timeout/connection error, or an HTTP 429 (rate limit) / 5xx (server error) response.

Deliberately narrow: this must NEVER retry a validation/schema failure (already handled by
each provider's own separate correction-prompt retry in generate_structured -- re-sending the
identical prompt without a correction would just reproduce the identical bad output) or a
genuine 4xx client error (bad API key, malformed request, model not found -- retrying an
unchanged request against an unchanged error condition cannot succeed, it only adds latency
and burns the caller's own request budget for nothing). Each provider supplies its own
`is_transient` classifier so this module stays provider-agnostic."""

import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

R = TypeVar("R")

# 1 initial attempt + 2 retries -- matches this task's "1-2 retries" bound. Short exponential
# backoff (0.2s, then 0.4s) so a real transient blip has a moment to clear without adding
# meaningful latency to a request the student is actively waiting on, and without materially
# slowing this app's own test suite (which exercises the retry path directly).
_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 0.2


def call_with_retry(fn: Callable[[], R], *, is_transient: Callable[[Exception], bool], context: str) -> R:
    """Call fn(), retrying up to `_MAX_ATTEMPTS - 1` more times with a short exponential
    backoff, but ONLY for exceptions `is_transient` reports as transient. Anything
    non-transient (or the final attempt's exception) is re-raised immediately with no delay."""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc) or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = _BASE_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "Transient AI provider error on attempt %d/%d (%s): %s -- retrying in %.1fs",
                attempt + 1, _MAX_ATTEMPTS, context, type(exc).__name__, delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover -- the loop always returns or raises
