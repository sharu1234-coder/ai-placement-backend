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
