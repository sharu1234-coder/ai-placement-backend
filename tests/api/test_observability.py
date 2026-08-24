import logging

from fastapi.testclient import TestClient

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
