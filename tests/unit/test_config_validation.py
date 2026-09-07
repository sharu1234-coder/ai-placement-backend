import pytest

from app.core.config import Settings, settings, validate_for_production


def _production_ready_ai(monkeypatch) -> None:
    """AI_PROVIDER defaults to 'ollama' at localhost, which validate_for_production now
    rejects in production (nothing would answer there, and every AI feature would silently
    degrade to template text). Tests below that are about *other* settings set a real cloud
    provider so they aren't coupled to that unrelated default."""
    monkeypatch.setattr(settings, "ai_provider", "anthropic")


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
    _production_ready_ai(monkeypatch)
    # This dev checkout's .env ships a real rzp_test_ key (needed for the Razorpay-flow
    # tests elsewhere) -- explicitly override it here so this "everything else is production-
    # ready" test isn't coupled to that unrelated local dev value.
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc123")
    # Required alongside a configured key since the webhook-secret check was added: without
    # it the webhook endpoint fails closed and paid users would never be activated.
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "a-real-webhook-secret")

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
    _production_ready_ai(monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_abc123")

    with pytest.raises(RuntimeError, match="RAZORPAY_KEY_ID"):
        validate_for_production()


def test_validate_for_production_passes_with_a_live_razorpay_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allowed_hosts", "api.aiplacementos.com")
    _production_ready_ai(monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc123")
    # Required alongside a configured key since the webhook-secret check was added: without
    # it the webhook endpoint fails closed and paid users would never be activated.
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "a-real-webhook-secret")

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
    _production_ready_ai(monkeypatch)
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


def _production_base(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allowed_hosts", "api.aiplacementos.com")
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc123")
    # Required alongside a configured key since the webhook-secret check was added: without
    # it the webhook endpoint fails closed and paid users would never be activated.
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "a-real-webhook-secret")


@pytest.mark.parametrize(
    "loopback_url",
    [
        "http://localhost:11434",  # the literal un-overridden default
        "http://127.0.0.1:11434",
        "http://127.1:11434",  # still 127.0.0.0/8
        "http://127.0.1.5:11434",
        "http://0.0.0.0:11434",
        "http://[::1]:11434",
    ],
)
def test_validate_for_production_raises_for_ollama_on_loopback(monkeypatch, loopback_url) -> None:
    """Real audit gap: AI_PROVIDER defaults to 'ollama' and OLLAMA_BASE_URL to localhost, and
    unlike ANTHROPIC_API_KEY/SMTP_*/STORAGE_S3_* that combination does NOT fail loudly on
    first use -- the connection error is caught as AIGenerationError and every call site falls
    back to hand-authored template text. A deployment that simply never set either would serve
    generic content from Career Path, Project Builder, Recruiter Attack, JD prep and answer
    grading forever, returning 200s the whole time with no startup error and no 5xx to alert
    on. Same silent-misconfiguration class as the rzp_test_ key check."""
    _production_base(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "ollama")
    monkeypatch.setattr(settings, "ollama_base_url", loopback_url)

    with pytest.raises(RuntimeError, match="AI_PROVIDER"):
        validate_for_production()


def test_validate_for_production_allows_a_genuinely_self_hosted_ollama(monkeypatch) -> None:
    """Deliberately NOT "ollama is forbidden in production" -- self-hosting Ollama at a real
    reachable address is a legitimate topology, and only the un-overridden dev default is
    rejected."""
    _production_base(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "ollama")
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama.internal:11434")

    validate_for_production()  # must not raise


def test_validate_for_production_ignores_ollama_url_when_provider_is_anthropic(monkeypatch) -> None:
    """A leftover localhost OLLAMA_BASE_URL is harmless once the provider is the cloud one --
    nothing reads it, so failing on it would be a false positive."""
    _production_base(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "ollama_base_url", "http://localhost:11434")

    validate_for_production()  # must not raise


def test_ollama_on_loopback_is_fine_in_development(monkeypatch) -> None:
    """The default dev checkout -- every developer runs exactly this and must not be blocked."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "ai_provider", "ollama")
    monkeypatch.setattr(settings, "ollama_base_url", "http://localhost:11434")

    validate_for_production()  # must not raise


def test_validate_for_production_requires_a_webhook_secret_when_razorpay_is_configured(
    monkeypatch,
) -> None:
    """A live key with no webhook secret is the quietest way to lose a customer's money.

    subscriptions.py fails the webhook closed without the secret -- correct, since it cannot
    otherwise tell Razorpay from anyone who finds the URL -- but that turns every
    payment.captured callback into a 503. The webhook is the path that survives the client
    never coming back (app killed, network dropped mid-redirect), so a student could be
    charged for real and never receive premium. Nothing else in the system complains: startup
    succeeds, checkout works, and client-side verify works in every manual test.
    """
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allowed_hosts", "api.aiplacementos.com")
    _production_ready_ai(monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc123")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", None)

    with pytest.raises(RuntimeError, match="RAZORPAY_WEBHOOK_SECRET"):
        validate_for_production()


def test_validate_for_production_does_not_require_a_webhook_secret_when_razorpay_is_unset(
    monkeypatch,
) -> None:
    """Payments being switched off entirely is a valid deployment -- subscription_service
    already fails loudly on first use. Only a *configured* gateway needs the webhook."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a-real-random-64-char-secret-not-the-dev-default")
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allowed_hosts", "api.aiplacementos.com")
    _production_ready_ai(monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", None)

    validate_for_production()  # must not raise
