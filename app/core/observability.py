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

# The status a request ends on when an exception escapes the application. Starlette's
# ServerErrorMiddleware turns any Exception that reaches it into a response of this status --
# through main.py's @app.exception_handler(Exception) here, Starlette's own error_response
# otherwise -- and it is the only layer sitting outside every app.add_middleware() layer,
# including this one. So that response never travels back through send_wrapper below and cannot
# be observed the way every other status is; it has to be recorded from this side. Named rather
# than inlined because it is a claim about another module's behaviour:
# tests/api/test_observability.py asserts the logged value equals the status the client is
# actually served, so the two cannot drift apart silently.
UNHANDLED_EXCEPTION_STATUS = 500


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

    The logged status is always the status the client actually received. "-" appears in that
    field only when no HTTP response was emitted at all -- a request cancelled by a client
    disconnect, where CancelledError is not an Exception and so ServerErrorMiddleware never
    turns it into a 500. That is the one condition with no status to report, so it is the one
    condition the placeholder describes; a request that failed *with* a status logs that status.

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
        except Exception:
            # Observing the failure, not handling it. The exception is re-raised untouched, so
            # ServerErrorMiddleware still builds the 500, main.py's handler still logs the
            # traceback, and the server still sees the original error -- the only thing that
            # happens here is that the status the client is about to be served gets recorded,
            # because the layer that emits it is outside this one (see
            # UNHANDLED_EXCEPTION_STATUS).
            #
            # Conditional because a response that has already started cannot be replaced: once
            # a status line is on the wire, ServerErrorMiddleware leaves it alone and only
            # re-raises, so the status already sent is what the client got and what must be
            # logged -- a 500 here would describe a response that was never emitted.
            if status_code == 0:
                status_code = UNHANDLED_EXCEPTION_STATUS
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            try:
                logger.log(
                    _log_level_for_duration(duration_ms),
                    "%s %s -> %s (%.1fms) [request_id=%s]",
                    scope.get("method", ""),
                    scope.get("path", ""),
                    status_code or "-",
                    duration_ms,
                    request_id,
                )
            except Exception:
                # Telemetry must never become the request's outcome. This runs in a finally
                # that is frequently unwinding an application exception, so an error raised
                # here would propagate in that exception's place -- destroying the failure it
                # was trying to describe. Deliberately swallowed, and deliberately the only
                # thing in this middleware that is.
                pass
