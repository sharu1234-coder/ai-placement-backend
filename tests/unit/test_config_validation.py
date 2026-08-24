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


@pytest.mark.parametrize("typoed_environment", ["Production", "PRODUCTION", "prod", "productoin"])
def test_validate_for_production_still_validates_a_typoed_environment_value(monkeypatch, typoed_environment) -> None:
    # Regression: this used to check `environment == "production"` exactly, so any typo'd
    # deployment value fell into the "not production" branch and silently skipped every check
    # below -- fail-open on the one setting whose entire job is catching an insecure
    # production deploy. Anything that isn't the known-safe "development" value must now be
    # validated, matching real production requirements.
    monkeypatch.setattr(settings, "environment", typoed_environment)
    monkeypatch.setattr(settings, "jwt_secret_key", Settings.model_fields["jwt_secret_key"].default)

    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        validate_for_production()
