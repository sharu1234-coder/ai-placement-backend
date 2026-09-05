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
    monkeypatch.setattr(settings, "allowed_hosts", "api.aiplacementos.com")
    # This dev checkout's .env ships a real rzp_test_ key (needed for the Razorpay-flow
    # tests elsewhere) -- explicitly override it here so this "everything else is production-
    # ready" test isn't coupled to that unrelated local dev value.
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc123")

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


def test_validate_for_production_raises_with_wildcard_allowed_hosts(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allowed_hosts", "*")

    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS"):
        validate_for_production()


def test_validate_for_production_raises_with_a_test_mode_razorpay_key(monkeypatch) -> None:
    """Real audit gap, now fixed: validate_for_production already fail-closes for an
    insecure JWT secret, console email provider, debug mode, and wildcard allowed-hosts, but
    had no equivalent check for a rzp_test_ key surviving into production -- a real
    deployment could go live with real user traffic hitting Razorpay's test mode, silently
    collecting no real money while appearing to work end-to-end."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allowed_hosts", "api.aiplacementos.com")
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_abc123")

    with pytest.raises(RuntimeError, match="RAZORPAY_KEY_ID"):
        validate_for_production()


def test_validate_for_production_passes_with_a_live_razorpay_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allowed_hosts", "api.aiplacementos.com")
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc123")

    validate_for_production()  # must not raise


def test_validate_for_production_does_not_require_razorpay_to_be_configured(monkeypatch) -> None:
    """Subscriptions being unconfigured (razorpay_key_id=None) already fails loudly on first
    real use elsewhere (subscription_service) -- this startup check must not additionally
    demand a key just to start the app, matching the same "fail loudly on first use, not at
    startup" treatment already given to ANTHROPIC_API_KEY/STORAGE_S3_*/SMTP_*."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allowed_hosts", "api.aiplacementos.com")
    monkeypatch.setattr(settings, "razorpay_key_id", None)

    validate_for_production()  # must not raise


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
