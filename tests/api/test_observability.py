import asyncio
import contextlib
import logging

import pytest
from fastapi.testclient import TestClient

from app.core import observability as observability_module
from app.core.observability import RequestObservabilityMiddleware
from app.main import app

client = TestClient(app)


def test_response_includes_a_generated_request_id() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    request_id = response.headers.get("x-request-id")
    assert request_id
    assert len(request_id) == 32  # uuid4().hex


def test_well_formed_client_supplied_request_id_is_echoed_back() -> None:
    response = client.get("/health", headers={"x-request-id": "client-supplied-id-123"})

    assert response.headers["x-request-id"] == "client-supplied-id-123"


def test_malformed_client_supplied_request_id_is_replaced() -> None:
    # Contains a character outside the allowed set and is used verbatim nowhere -- must not
    # be echoed back or trusted, since it flows into log lines and the response header.
    response = client.get("/health", headers={"x-request-id": "not valid! " + ("x" * 100)})

    request_id = response.headers.get("x-request-id")
    assert request_id
    assert request_id != "not valid! " + ("x" * 100)
    assert len(request_id) == 32


def test_two_requests_get_different_generated_request_ids() -> None:
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]

    assert first != second


def test_unhandled_exception_response_includes_request_id(monkeypatch) -> None:
    from app.api import legal as legal_module

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(legal_module, "_read_legal_document", boom)

    # This client must not re-raise the simulated server exception (TestClient's default) --
    # the whole point of this test is to see the JSONResponse main.py's catch-all handler
    # actually produces.
    non_raising_client = TestClient(app, raise_server_exceptions=False)
    response = non_raising_client.get("/legal/privacy-policy")

    assert response.status_code == 500
    body = response.json()
    assert body["request_id"]
    assert body["request_id"] == response.headers["x-request-id"]


def test_request_is_logged_with_method_path_status_and_request_id(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        response = client.get("/health")

    request_id = response.headers["x-request-id"]
    matching = [r for r in caplog.records if request_id in r.getMessage()]
    assert len(matching) == 1
    message = matching[0].getMessage()
    assert "GET" in message
    assert "/health" in message
    assert "200" in message


def test_log_level_for_duration_is_warning_at_and_above_the_slow_threshold() -> None:
    # Deliberately not tested by mocking time.perf_counter and driving a real request through
    # the middleware: time is a process-wide module also used by asyncio/anyio internals, and
    # patching it globally made unrelated code in the same request's call stack observe the
    # fake clock too (caused real StopIteration failures when tried). This pure function is
    # exactly what the middleware's slow/fast decision reduces to, so testing it directly is
    # both simpler and actually correct.
    from app.core.observability import SLOW_REQUEST_THRESHOLD_MS, _log_level_for_duration

    assert _log_level_for_duration(SLOW_REQUEST_THRESHOLD_MS - 1) == logging.INFO
    assert _log_level_for_duration(SLOW_REQUEST_THRESHOLD_MS) == logging.WARNING
    assert _log_level_for_duration(SLOW_REQUEST_THRESHOLD_MS + 1000) == logging.WARNING


# ==========================================================================================
# F12-09: the logged status must be the status the client actually received
#
# Every test below asserts the log against the *response*, not against a hard-coded number.
# That is the point: a test that only checks "the log says 500" passes just as happily when the
# client is served something else, which is the class of defect this section exists to catch.
# ==========================================================================================


def _headers(client, email: str) -> dict:
    """Sign up and return this account's bearer header -- the same three lines every other API
    test file in this suite uses."""
    client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": email, "password": "supersecret123"}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


def _status_logged_for(caplog, request_id: str) -> str:
    """The status field of the access-log line for one specific request."""
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.core.observability" and request_id in record.getMessage()
    ]
    assert len(lines) == 1, f"expected exactly one access-log line for {request_id}, got {lines}"
    # "GET /path -> 500 (1.2ms) [request_id=...]" -- the field between the arrow and the timing.
    return lines[0].rsplit(" -> ", 1)[1].split(" ", 1)[0]


def _line_logged_for(caplog, request_id: str) -> str:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.core.observability" and request_id in record.getMessage()
    ][0]


@contextlib.contextmanager
def _failing_legal_document(monkeypatch):
    """A genuine unhandled exception from a genuine route, through the same seam
    test_unhandled_exception_response_includes_request_id already uses -- so no test-only route
    is added to the application to produce one."""
    from app.api import legal as legal_module

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(legal_module, "_read_legal_document", boom)
    yield


def test_an_unhandled_exception_logs_the_500_the_client_actually_received(caplog, monkeypatch) -> None:
    """F12-09. The response is built by ServerErrorMiddleware, which sits outside this
    middleware, so before the fix the status was never observed and the access log read "-" for
    a request the client saw fail with a 500."""
    request_id = "f1209-unhandled-500"
    non_raising_client = TestClient(app, raise_server_exceptions=False)

    with _failing_legal_document(monkeypatch), caplog.at_level(logging.INFO, logger="app.core.observability"):
        response = non_raising_client.get("/legal/privacy-policy", headers={"x-request-id": request_id})

    assert response.status_code == 500
    logged = _status_logged_for(caplog, request_id)
    assert logged == "500"
    assert logged == str(response.status_code)
    assert logged != "-"


def test_an_unhandled_exception_still_reaches_the_server_untouched(caplog, monkeypatch) -> None:
    """The status is recorded by observing the exception, not by handling it: the original error
    must still escape, or uvicorn loses the traceback and a test client can no longer fail a
    test on it."""
    request_id = "f1209-propagation"
    raising_client = TestClient(app)  # TestClient's default: re-raise, like a real server

    with _failing_legal_document(monkeypatch), caplog.at_level(logging.INFO, logger="app.core.observability"):
        with pytest.raises(RuntimeError, match="simulated failure"):
            raising_client.get("/legal/privacy-policy", headers={"x-request-id": request_id})

    assert _status_logged_for(caplog, request_id) == "500"


def test_the_failure_log_line_keeps_method_path_duration_and_request_id(caplog, monkeypatch) -> None:
    """A correct status is no use if the failing request stops being identifiable."""
    request_id = "f1209-correlation"

    with _failing_legal_document(monkeypatch), caplog.at_level(logging.INFO, logger="app.core.observability"):
        TestClient(app, raise_server_exceptions=False).get(
            "/legal/privacy-policy", headers={"x-request-id": request_id}
        )

    line = _line_logged_for(caplog, request_id)
    assert "GET" in line
    assert "/legal/privacy-policy" in line
    assert "ms)" in line
    assert f"[request_id={request_id}]" in line


def test_exactly_one_access_log_line_is_written_for_a_failed_request(caplog, monkeypatch) -> None:
    """One request, one access record. The fix records the status on the exception path, which
    is also the path a duplicate entry would appear on."""
    request_id = "f1209-single-line"

    with _failing_legal_document(monkeypatch), caplog.at_level(logging.INFO, logger="app.core.observability"):
        TestClient(app, raise_server_exceptions=False).get(
            "/legal/privacy-policy", headers={"x-request-id": request_id}
        )

    lines = [
        r.getMessage() for r in caplog.records
        if r.name == "app.core.observability" and request_id in r.getMessage()
    ]
    assert len(lines) == 1


def test_the_logged_request_id_matches_the_one_the_client_is_told_to_report(monkeypatch) -> None:
    """The 500 body carries a request_id so a student can quote it in a support message; that is
    only useful if it is the id the access log recorded for the same request."""
    with _failing_legal_document(monkeypatch):
        response = TestClient(app, raise_server_exceptions=False).get("/legal/privacy-policy")

    assert response.json()["request_id"] == response.headers["x-request-id"]


# ------------------------------------------------------------------------------------------
# The statuses that were already correct, pinned so the fix cannot have moved them
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "method", "path", "body"),
    [
        (200, "GET", "/health", None),
        (404, "GET", "/no-such-route-exists", None),
        (401, "GET", "/users/me", None),
        (422, "POST", "/auth/signup", {"email": "not-an-email"}),
    ],
)
def test_ordinary_statuses_are_logged_exactly_as_the_client_received_them(
    client, caplog, expected, method, path, body
) -> None:
    request_id = f"f1209-{expected}"

    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        response = client.request(method, path, json=body, headers={"x-request-id": request_id})

    assert response.status_code == expected
    assert _status_logged_for(caplog, request_id) == str(response.status_code)


def test_a_201_is_logged_as_201(client, caplog) -> None:
    request_id = "f1209-201"

    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        response = client.post(
            "/auth/signup",
            json={"email": "obs-201@example.com", "password": "supersecret123"},
            headers={"x-request-id": request_id},
        )

    assert response.status_code == 201
    assert _status_logged_for(caplog, request_id) == "201"


def test_a_204_is_logged_as_204(client, caplog) -> None:
    """A 204 carries no body at all -- worth its own case, since a response with nothing after
    the status line is exactly the shape a careless wrapper mishandles."""
    headers = _headers(client, "obs-204@example.com")
    goal = client.post(
        "/career-goals",
        json={
            "title": "Data Engineer",
            "company": "Microsoft",
            "location": "Dubai",
            "job_description": "Build and operate data pipelines in Python and SQL. " * 5,
        },
        headers=headers,
    ).json()
    request_id = "f1209-204"

    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        response = client.delete(
            f"/career-goals/{goal['id']}", headers={**headers, "x-request-id": request_id}
        )

    assert response.status_code == 204
    assert _status_logged_for(caplog, request_id) == "204"


def test_a_403_is_logged_as_403(client, caplog) -> None:
    """A free caller reaching a Premium endpoint -- an authorisation refusal, not a failure."""
    headers = _headers(client, "obs-403@example.com")
    request_id = "f1209-403"

    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        response = client.post(
            "/career-decisions/simulate",
            json={"role_ids": [1, 2]},
            headers={**headers, "x-request-id": request_id},
        )

    assert response.status_code == 403
    assert _status_logged_for(caplog, request_id) == "403"


def test_a_400_is_logged_as_400(client, caplog) -> None:
    request_id = "f1209-400"

    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        response = client.post(
            "/auth/reset-password",
            json={"email": "nobody@example.com", "code": "000000", "new_password": "supersecret123"},
            headers={"x-request-id": request_id},
        )

    assert response.status_code == 400
    assert _status_logged_for(caplog, request_id) == "400"


def test_a_rate_limited_request_is_logged_as_429(client, caplog) -> None:
    """429 comes from SlowAPIMiddleware, which runs inside this one, so it arrives as an
    ordinary response -- and must stay that way now that the exception path sets a status too."""
    request_id = "f1209-429"
    last = None
    for _ in range(12):
        last = client.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong"})
        if last.status_code == 429:
            break
    assert last is not None and last.status_code == 429, "could not trip the login rate limit"

    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        response = client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "wrong"},
            headers={"x-request-id": request_id},
        )

    assert response.status_code == 429
    assert _status_logged_for(caplog, request_id) == "429"


def test_a_503_is_logged_as_503_and_not_flattened_into_500(client, caplog) -> None:
    """An explicit server status is not the unhandled-exception status. Readiness answers 503
    when the database is behind the code -- if the exception path were applied to every server
    failure, this would quietly become 500 and hide which kind of failure happened."""
    from sqlalchemy import text as sql_text

    from app.api.deps import get_db

    # Written through the same session factory the route will be handed, not through
    # app.database.session.engine -- the client fixture binds its own engine, so a row written
    # to the configured one would be invisible to the request and readiness would answer 200.
    session = next(app.dependency_overrides[get_db]())
    session.execute(sql_text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))"))
    session.execute(sql_text("DELETE FROM alembic_version"))
    session.execute(sql_text("INSERT INTO alembic_version (version_num) VALUES ('0000notthehead')"))
    session.commit()
    request_id = "f1209-503"

    try:
        with caplog.at_level(logging.INFO, logger="app.core.observability"):
            response = client.get("/health/ready", headers={"x-request-id": request_id})
    finally:
        session.execute(sql_text("DROP TABLE IF EXISTS alembic_version"))
        session.commit()
        session.close()

    assert response.status_code == 503
    assert _status_logged_for(caplog, request_id) == "503"


# ------------------------------------------------------------------------------------------
# The two cases no route in this app can produce, driven at the middleware itself. Nothing
# here returns a StreamingResponse and nothing lets a test cancel a live request, but the
# middleware still has to answer both correctly -- and both are ways this fix could have been
# written wrong while still passing every test above.
# ------------------------------------------------------------------------------------------


def _drive(inner, request_id: str):
    """Run one request through RequestObservabilityMiddleware wrapped around a bare ASGI app."""
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/seam",
        "headers": [(b"x-request-id", request_id.encode())],
    }
    raised = None
    try:
        asyncio.run(RequestObservabilityMiddleware(inner)(scope, receive, send))
    except BaseException as exc:  # noqa: B036 -- handed back to the test, not handled here
        raised = exc
    return [m["status"] for m in sent if m["type"] == "http.response.start"], raised


def test_a_response_that_already_started_keeps_the_status_the_client_was_sent(caplog) -> None:
    """Once the status line is on the wire, ServerErrorMiddleware cannot replace it -- it only
    re-raises. Logging 500 here would describe a response that was never emitted."""

    async def starts_then_fails(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("failed after the status line was already sent")

    request_id = "f1209-already-started"
    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        statuses, raised = _drive(starts_then_fails, request_id)

    assert statuses == [200]
    assert isinstance(raised, RuntimeError)
    assert _status_logged_for(caplog, request_id) == "200"


def test_a_cancelled_request_reports_no_status_rather_than_inventing_one(caplog) -> None:
    """A client that disconnects is served nothing at all: CancelledError is not an Exception,
    so ServerErrorMiddleware never turns it into a 500. "-" is the honest answer here, and this
    is the one condition it is allowed to describe."""

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError()

    request_id = "f1209-cancelled"
    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        statuses, raised = _drive(cancelled, request_id)

    assert statuses == []
    assert isinstance(raised, asyncio.CancelledError)
    assert _status_logged_for(caplog, request_id) == "-"


# ------------------------------------------------------------------------------------------
# Logging must never become the request's outcome
# ------------------------------------------------------------------------------------------


def _broken_log(*args, **kwargs):
    raise RuntimeError("the logging backend is down")


def test_a_broken_logger_cannot_change_a_successful_response(monkeypatch) -> None:
    monkeypatch.setattr(observability_module.logger, "log", _broken_log)

    assert client.get("/health").status_code == 200


def test_a_broken_logger_cannot_replace_the_application_exception(monkeypatch) -> None:
    """The log call runs inside a finally that is unwinding the application's exception. If it
    raised, the logging error would surface in place of the real failure -- the original error
    destroyed by the very code meant to record it."""
    with _failing_legal_document(monkeypatch):
        monkeypatch.setattr(observability_module.logger, "log", _broken_log)
        with pytest.raises(RuntimeError, match="simulated failure"):
            TestClient(app).get("/legal/privacy-policy")


def test_a_broken_logger_still_lets_the_client_have_its_500(monkeypatch) -> None:
    with _failing_legal_document(monkeypatch):
        monkeypatch.setattr(observability_module.logger, "log", _broken_log)
        response = TestClient(app, raise_server_exceptions=False).get("/legal/privacy-policy")

    assert response.status_code == 500


# ------------------------------------------------------------------------------------------
# Hygiene: a more accurate status must not arrive with more exposure
# ------------------------------------------------------------------------------------------


def test_the_access_log_line_carries_no_credentials_or_body_content(client, caplog) -> None:
    request_id = "f1209-hygiene"
    secret_password = "correct-horse-battery-staple"

    with caplog.at_level(logging.INFO, logger="app.core.observability"):
        client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": secret_password},
            headers={"x-request-id": request_id, "authorization": "Bearer super-secret-token"},
        )

    line = _line_logged_for(caplog, request_id)
    assert secret_password not in line
    assert "super-secret-token" not in line
    assert "nobody@example.com" not in line


def test_an_unhandled_failure_logs_no_exception_detail_in_the_access_line(caplog, monkeypatch) -> None:
    """The traceback belongs to main.py's handler, which already logs it once. This line stays a
    single structured access record -- the status is what changed, not the volume."""
    request_id = "f1209-no-detail"

    with _failing_legal_document(monkeypatch), caplog.at_level(logging.INFO, logger="app.core.observability"):
        TestClient(app, raise_server_exceptions=False).get(
            "/legal/privacy-policy", headers={"x-request-id": request_id}
        )

    line = _line_logged_for(caplog, request_id)
    assert "simulated failure" not in line
    assert "RuntimeError" not in line
    assert "Traceback" not in line
