from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)
https_client = TestClient(app, base_url="https://testserver")


def test_normal_request_still_works_with_default_allowed_hosts() -> None:
    # Default ALLOWED_HOSTS="*" -- TrustedHostMiddleware must not reject ordinary requests
    # out of the box (regression safety for the middleware wiring itself).
    response = client.get("/health")
    assert response.status_code == 200


def test_no_hsts_header_over_plain_http() -> None:
    response = client.get("/health")
    assert "strict-transport-security" not in response.headers


def test_no_hsts_header_in_development_even_over_https() -> None:
    # settings.environment defaults to "development" in tests.
    response = https_client.get("/health")
    assert "strict-transport-security" not in response.headers


def test_hsts_header_present_in_production_over_https(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")

    response = https_client.get("/health")

    assert response.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"


def test_no_hsts_header_in_production_over_plain_http(monkeypatch) -> None:
    # Never advertise HSTS over a connection that wasn't actually secure.
    monkeypatch.setattr(settings, "environment", "production")

    response = client.get("/health")

    assert "strict-transport-security" not in response.headers


def test_declared_oversized_body_rejected_before_reaching_the_route(client) -> None:
    # Fast path (app/core/body_size_limit.py): an honestly-declared Content-Length over the
    # cap is rejected off the header alone, before the body is even read -- so this works
    # even though the actual bytes sent here are tiny.
    response = client.post(
        "/auth/signup",
        content=b"{}",
        headers={"content-length": str(13 * 1024 * 1024), "content-type": "application/json"},
    )
    assert response.status_code == 413


def test_streamed_oversized_body_rejected_without_declared_length(client) -> None:
    # Slow path: a body sent without a (truthful) Content-Length -- e.g. chunked transfer --
    # must still be cut off once the actual streamed bytes exceed the cap, not just trusted
    # because no size was declared upfront.
    def oversized_chunks():
        chunk = b"a" * (1024 * 1024)
        for _ in range(13):
            yield chunk

    response = client.post(
        "/auth/signup",
        content=oversized_chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
