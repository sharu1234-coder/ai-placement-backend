from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings


class HSTSMiddleware:
    """Adds Strict-Transport-Security only for requests that actually arrived over HTTPS in
    production -- reads scope["scheme"] fresh per request (not baked in at import), so it
    reflects reality even behind a reverse proxy (requires Uvicorn's --proxy-headers flag to
    correctly report "https" here -- see README). Harmless no-op in dev, where every request
    is plain HTTP.

    Deliberately plain ASGI, not @app.middleware("http")/BaseHTTPMiddleware: BaseHTTPMiddleware
    has a documented limitation where an exception raised while the app reads the request body
    (e.g. app/core/body_size_limit.py's streaming size check) doesn't propagate as a normal
    HTTP error response if it's nested inside a BaseHTTPMiddleware's call_next() -- it surfaces
    as an unhandled exception instead. Same external behavior as the original
    @app.middleware("http") version; this is an implementation-only fix, found by that
    interaction actually breaking body_size_limit.py's own tests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        add_header = settings.environment == "production" and scope.get("scheme") == "https"

        async def send_wrapper(message: Message) -> None:
            if add_header and message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
            await send(message)

        await self.app(scope, receive, send_wrapper)
