from starlette.types import ASGIApp, Receive, Scope, Send

_BODY_TOO_LARGE_RESPONSE = b'{"detail":"Request body too large"}'


class MaxBodySizeMiddleware:
    """Pure-ASGI request-body size cap (no new dependency). Before this, every JSON-body
    route (auth, jobs, assessments, roadmap, profile items, ...) had no size limit at all --
    Starlette/Uvicorn would buffer an arbitrarily large body before Pydantic validation ever
    ran. The two upload routes (resumes, interview audio) already stream-enforce their own
    10MB cap (app/api/upload_utils.py) well under this ceiling, so this is a blunt global
    backstop for everything else, not a replacement for those per-route checks.

    Two layers, because either alone is bypassable:
    - An honest Content-Length header lets us reject before reading any body at all, and
      before the inner app (and its dependencies, like a DB session) ever runs.
    - A client that omits Content-Length (chunked transfer) is bounded by counting bytes as
      they stream in via `receive()`; once the cap is exceeded, `receive()` starts reporting
      the body as already finished (rather than continuing to pull from the real connection),
      and `send()` is intercepted so whatever the inner app produces from that now-truncated
      body (typically a 422 from Pydantic choking on incomplete JSON) never reaches the
      client -- our own 413 is sent instead.

    Deliberately does NOT raise an exception to signal the oversized case (an earlier version
    did): a plain `add_middleware()`-registered middleware sits outside Starlette's
    ExceptionMiddleware (which only wraps the router), so an exception raised here isn't
    converted into a normal HTTP response the way one raised inside a route handler or
    dependency is -- it surfaces as an unhandled 500 instead. Constructing the response
    directly here avoids depending on that machinery at all.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_size:
                    await self._send_413(send)
                    return
            except ValueError:
                pass

        total = 0
        oversized = False

        async def guarded_receive():
            nonlocal total, oversized
            if oversized:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_size:
                    oversized = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        sent_413 = False

        async def guarded_send(message):
            nonlocal sent_413
            if oversized:
                if not sent_413:
                    sent_413 = True
                    await self._send_413(send)
                return
            await send(message)

        await self.app(scope, guarded_receive, guarded_send)

    @staticmethod
    async def _send_413(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": _BODY_TOO_LARGE_RESPONSE})
