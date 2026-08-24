import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Generous on purpose: AI-inference-backed endpoints can legitimately take well over a
# second (see NetworkModule's 150s Android read timeout for that same reason). This only
# ever affects which log level a request is recorded at -- never blocks or rejects anything.
SLOW_REQUEST_THRESHOLD_MS = 3000

# A client-supplied X-Request-ID is trusted only if it looks like a plausible ID -- keeps an
# arbitrary/oversized header value out of logs and out of the response we echo it back in.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _resolve_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers") or []:
        if name == b"x-request-id":
            candidate = value.decode("latin-1", errors="ignore")
            if _VALID_REQUEST_ID.match(candidate):
                return candidate
            break
    return uuid.uuid4().hex


def _log_level_for_duration(duration_ms: float) -> int:
    return logging.WARNING if duration_ms >= SLOW_REQUEST_THRESHOLD_MS else logging.INFO


class RequestObservabilityMiddleware:
    """Assigns every request a correlation ID (request.state.request_id, echoed back as the
    X-Request-ID response header) and logs method/path/status/duration for every request --
    the "request correlation IDs" gap flagged as a good future addition in item 16's README
    entry. Reuses a well-formed client-supplied X-Request-ID if present (lets Android or a
    future service propagate one end-to-end); generates one otherwise.

    Deliberately plain ASGI, matching HSTSMiddleware/MaxBodySizeMiddleware in this package
    (see security_headers.py's docstring for why BaseHTTPMiddleware is avoided here). Added
    as the outermost middleware in main.py so it observes and times the final response to
    every request, including ones rejected by TrustedHostMiddleware, the rate limiter, or the
    body-size cap -- not just ones that reach a route.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id

        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.log(
                _log_level_for_duration(duration_ms),
                "%s %s -> %s (%.1fms) [request_id=%s]",
                scope.get("method", ""),
                scope.get("path", ""),
                status_code or "-",
                duration_ms,
                request_id,
            )
