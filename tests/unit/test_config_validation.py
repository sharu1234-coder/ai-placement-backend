import pytest

from app.core.config import Settings, settings, validate_for_production


def test_validate_for_production_is_a_noop_outside_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "jwt_secret_key", Settings.model_fields["jwt_secret_key"].default)

    validate_for_production()  # must not raise


def test_validate_for_production_raises_with_the_default_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", Settings.model_fields["jwt_secret_key"].default)

    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        validate_for_production()


def test_validate_for_production_passes_with_a_real_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)

    validate_for_production()  # must not raise


def test_validate_for_production_raises_with_console_email_provider(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "console")
    monkeypatch.setattr(settings, "debug", False)

    with pytest.raises(RuntimeError, match="EMAIL_PROVIDER"):
        validate_for_production()


def test_validate_for_production_raises_with_debug_true(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", True)

    with pytest.raises(RuntimeError, match="DEBUG"):
        validate_for_production()
