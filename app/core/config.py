from typing import Optional
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "AI Placement OS Backend"
    environment: str = "development"
    debug: bool = True

    database_url: str = "sqlite:///./app.db"

    # Dev-only default. Must be overridden via JWT_SECRET_KEY in .env before any real deployment.
    jwt_secret_key: str = "dev-only-insecure-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    ai_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    # A hardcoded 120s was too tight for larger structured-output calls (e.g. a multi-day
    # roadmap's task list) on modest dev hardware -- found via a live smoke test, not a
    # guess. Override via OLLAMA_TIMEOUT_SECONDS in .env if a given deployment needs more.
    ollama_timeout_seconds: int = 240

    # Production cloud provider (AI_PROVIDER=anthropic). Never hardcode a real key here --
    # this default is None so a misconfigured deployment fails loudly at first use rather
    # than silently trying an empty string. claude-sonnet-5 is the documented pick for
    # high-volume production workloads (vs. claude-opus-5's higher per-token cost) --
    # override via ANTHROPIC_MODEL in .env per-deployment if a different quality/cost
    # tradeoff is wanted (e.g. Opus for interview evaluation specifically, Haiku for cheap
    # extraction) -- see README "Production AI provider" for the full tradeoff.
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-5"
    # The Anthropic SDK's own default (10 minutes) has no app-level knob and is far longer
    # than this app's sync routes should ever tie up a threadpool worker for one AI call --
    # matches the explicit-timeout pattern OllamaProvider already uses. Override via
    # ANTHROPIC_TIMEOUT_SECONDS in .env if a given deployment needs more.
    anthropic_timeout_seconds: int = 120

    # Dev-only local STT for OllamaProvider.transcribe -- swap for a cloud STT provider
    # before real user testing (see CloudProvider note in README).
    whisper_model_size: str = "tiny.en"

    upload_dir: str = "uploads"

    # STORAGE_PROVIDER=local (dev default, writes under upload_dir) or "s3" for production.
    # S3StorageProvider speaks the plain S3 API, so it works against real AWS S3 or any
    # S3-compatible service (Cloudflare R2, MinIO, Backblaze B2, DigitalOcean Spaces, ...)
    # -- which vendor to use in production hasn't been decided yet; set
    # STORAGE_S3_ENDPOINT_URL to point at whichever one is chosen (leave unset for AWS).
    storage_provider: str = "local"
    storage_s3_bucket: Optional[str] = None
    storage_s3_region: Optional[str] = None
    storage_s3_endpoint_url: Optional[str] = None
    storage_s3_access_key_id: Optional[str] = None
    storage_s3_secret_access_key: Optional[str] = None

    # EMAIL_PROVIDER=console (dev default, logs the reset code instead of sending real
    # email) or "smtp" for production. SMTPEmailProvider speaks plain SMTP, so it works
    # with any SMTP-speaking service (Gmail app passwords, SendGrid/SES/Postmark/Mailgun
    # SMTP relays, ...) -- which vendor to use in production hasn't been decided yet.
    email_provider: str = "console"
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_address: Optional[str] = None
    password_reset_token_expire_minutes: int = 15

    # TEST MODE Razorpay credentials (rzp_test_... key id, its matching secret). None means
    # subscriptions are unconfigured -- create_order fails loudly with a clear error rather
    # than silently trying an empty auth header, same "fail loudly on first use" pattern as
    # anthropic_api_key/STORAGE_S3_*/SMTP_* above. Never hardcode a real value here, and
    # never send razorpay_key_secret to the Android client -- only razorpay_key_id (the
    # publishable half Razorpay's own Checkout SDK requires client-side) is ever returned
    # from an API response (see /subscriptions/checkout).
    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    # Separate from razorpay_key_secret above -- configured independently in the Razorpay
    # dashboard's Webhooks section, used only to verify X-Razorpay-Signature on inbound
    # webhook calls (see app/api/subscriptions.py's /webhook route). None means the webhook
    # route fails closed (503) rather than processing unauthenticated payloads.
    razorpay_webhook_secret: Optional[str] = None

    # JOB_PROVIDER selects the Job Hunt job source, exactly like storage_provider/
    # email_provider above. "none" (the default) is a real, deliberate state, not a broken
    # one: Job Hunt then answers with a specific "not configured" 503 that the Android client
    # renders as "Job Hunt isn't available yet". It must never degrade into an empty result
    # list, which would tell a student there are no jobs for them -- a claim about the labour
    # market that no unconfigured deployment has any evidence for.
    #
    # Both real options carry owner obligations that are NOT settled by setting a key here,
    # and are recorded in the Job Hunt audit report as owner decisions:
    #   * "adzuna"    -- commercial use beyond a 14-day evaluation requires written consent
    #                    from Adzuna, and each advert must be labelled "Jobs by Adzuna" with a
    #                    link back (developer.adzuna.com/docs/terms_of_service).
    #   * "careerjet" -- requires a partner account for the affiliate id, and contractually
    #                    requires sending each END USER's IP and user agent on every query,
    #                    which is a real privacy disclosure to a third party.
    job_provider: str = "none"
    adzuna_app_id: Optional[str] = None
    adzuna_app_key: Optional[str] = None
    # Adzuna is per-country; "in" is India. Changing this changes which market students see.
    adzuna_country: str = "in"
    careerjet_affid: Optional[str] = None
    careerjet_locale_code: str = "en_IN"

    # Comma-separated Host header allowlist (TrustedHostMiddleware, app/main.py). "*" (dev
    # default) accepts any Host -- fine locally, but a production deployment should set
    # this to its real domain(s) (e.g. "api.aiplacementos.com") to reject Host-header
    # spoofing/cache-poisoning attempts. TLS itself is terminated at the reverse proxy /
    # deployment platform (nginx, Render, Railway, an AWS ALB, ...), not by this app --
    # see README "HTTPS / network security" for the full picture, including why Uvicorn
    # needs --proxy-headers when run behind one of those.
    allowed_hosts: str = "*"

    # Off by default so enabling Firebase Admin never breaks an existing deployment that
    # hasn't set up a service account yet -- see app/core/app_check.py. When true, every
    # request (except the public /health*, /legal/* routes) must carry a valid
    # X-Firebase-AppCheck token (see NetworkModule.kt's AppCheckInterceptor on the Android
    # side) or is rejected with 401. Verification itself uses Google Application Default
    # Credentials (the standard GOOGLE_APPLICATION_CREDENTIALS env var pointing at a Firebase
    # service account JSON) -- there is no separate settings field for it, since that's
    # already how every other Google/Firebase client library in this ecosystem picks up
    # credentials, and duplicating it here would just be another place it could drift.
    firebase_app_check_enabled: bool = False


settings = Settings()

# Hosts that only ever resolve to the API process's own machine/container. Matched on the URL
# host specifically (not a substring of the whole URL) so a legitimate deployment whose real
# Ollama host merely *contains* one of these strings isn't caught.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_loopback_url(url: str) -> bool:
    host = urlparse(url).hostname
    if host is None:
        return False
    host = host.strip("[]").lower()
    # 127.0.0.0/8 as a whole, not just 127.0.0.1 -- 127.1 and 127.0.1.5 are equally loopback.
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def validate_for_production() -> None:
    """Called once at app startup (app/main.py). Every secret-shaped setting
    (ANTHROPIC_API_KEY, STORAGE_S3_*, SMTP_*) already fails loudly on first real use if left
    unconfigured -- their provider classes raise immediately (AnthropicProvider,
    S3StorageProvider, SMTPEmailProvider). The checks below cover settings that instead have
    a *safe-looking but production-unsafe* default -- nothing else would ever complain about
    them, so a production deployment could silently start in a state every dev checkout of
    this codebase also starts in: an insecure hardcoded JWT secret, reset codes logged in
    plaintext (EMAIL_PROVIDER=console), or verbose DEBUG-level logging left on.

    Skips only for the one known-safe non-production value ("development", the default for
    every dev checkout and the whole test suite) rather than checking `== "production"` --
    a deployment misconfigured with a typo'd ENVIRONMENT ("Production", "prod", ...) used to
    fail *open* here (silently skip every check below) rather than fail *closed*. There is no
    "staging"/"test" environment concept in this app yet; if one is added later, it needs an
    explicit exemption added here too."""
    if settings.environment == "development":
        return
    if settings.jwt_secret_key == Settings.model_fields["jwt_secret_key"].default:
        raise RuntimeError(
            "JWT_SECRET_KEY is still the insecure development default -- set a real, "
            "random secret before running in production, e.g.: "
            'python -c "import secrets; print(secrets.token_urlsafe(64))"'
        )
    if settings.email_provider == "console":
        # ConsoleEmailProvider logs password reset codes in plaintext (app/email/
        # console_provider.py) -- fine for local dev where nothing else can receive the
        # code, but in production that would put every user's reset code straight into
        # application logs.
        raise RuntimeError(
            "EMAIL_PROVIDER is still 'console' (logs reset codes in plaintext) -- set "
            "EMAIL_PROVIDER=smtp and the SMTP_* settings before running in production."
        )
    if settings.debug:
        # settings.debug also controls the app's own logging verbosity (app/core/
        # logging.py) -- DEBUG-level logging in production is noisier than intended, not a
        # direct secrets leak by itself, but combined with any future debug-only logging
        # it's a control worth enforcing explicitly rather than trusting it was remembered.
        raise RuntimeError("DEBUG is still true -- set DEBUG=false before running in production.")
    if settings.allowed_hosts == "*":
        # TrustedHostMiddleware (app/main.py) accepts any Host header when this is "*" --
        # fine locally, but in production it allows Host-header spoofing / cache-poisoning
        # attempts. Same fail-closed treatment as the three checks above rather than relying
        # on a deployment remembering to set it.
        raise RuntimeError(
            "ALLOWED_HOSTS is still '*' -- set it to your real production domain(s) "
            '(comma-separated, e.g. "api.aiplacementos.com") before running in production.'
        )
    if settings.ai_provider == "ollama" and _is_loopback_url(settings.ollama_base_url):
        # Same failure class as the test-mode Razorpay key below, and the same reason it needs
        # an explicit check: nothing else complains. AI_PROVIDER defaults to "ollama" and
        # OLLAMA_BASE_URL defaults to localhost, so a deployment that simply never set either
        # starts cleanly, then fails every AI call with a connection error -- and every call
        # site in app/services/ catches AIGenerationError and falls back to hand-authored
        # template text, so the API keeps returning 200s with generic content forever. Career
        # Path, Project Builder, Recruiter Attack, JD prep and answer grading would all
        # silently degrade for every user with no startup error and no 5xx to alert on.
        #
        # Deliberately NOT "ollama is forbidden in production": a genuinely self-hosted Ollama
        # is a legitimate topology. Only the un-overridden dev default (loopback) fails here.
        raise RuntimeError(
            "AI_PROVIDER is 'ollama' with OLLAMA_BASE_URL still pointing at localhost "
            f"({settings.ollama_base_url}) -- nothing on the API host will answer there in "
            "production, and every AI feature would silently fall back to generic template "
            "text instead of failing loudly. Either set AI_PROVIDER=anthropic (with "
            "ANTHROPIC_API_KEY), or, if you genuinely self-host Ollama, set OLLAMA_BASE_URL "
            "to its real reachable address (a hostname or service address the API can dial) "
            "-- from inside a container, localhost is the API container itself, not Ollama."
        )
    if settings.razorpay_key_id is not None and settings.razorpay_key_id.startswith("rzp_test_"):
        # Unlike the checks above, a missing/None razorpay_key_id is left alone here -- that
        # already fails loudly on first real use (subscription_service's own "Razorpay is not
        # configured" error), matching the same "fail loudly on first use" treatment this file's
        # own docstring already applies to ANTHROPIC_API_KEY/STORAGE_S3_*/SMTP_*. What has no
        # such safety net is a *configured* key that's silently still in test mode: a
        # `rzp_test_...` key id surviving into a production deployment would accept real user
        # traffic while every "payment" is actually a free Razorpay test-mode transaction --
        # the checkout would appear to work end-to-end while collecting no real money.
        raise RuntimeError(
            "RAZORPAY_KEY_ID is still a test-mode key ('rzp_test_...') -- set it to your real "
            "live-mode key ('rzp_live_...') before running in production."
        )
    if settings.razorpay_key_id is not None and not settings.razorpay_webhook_secret:
        # Same "nothing else complains" class as the two checks above. app/api/subscriptions.py
        # fails the webhook closed when this is unset -- correctly, since without the secret it
        # cannot tell Razorpay from anyone who finds the URL -- but that means EVERY webhook
        # call gets a 503. Razorpay's payment.captured callback is the path that survives the
        # client never coming back (app closed, network dropped, browser killed mid-redirect),
        # so with payments configured and this missing, a student can be charged for real and
        # never receive premium, with nothing failing at startup to warn anyone. Checkout and
        # the client-side verify would both look fine in testing.
        raise RuntimeError(
            "RAZORPAY_KEY_ID is configured but RAZORPAY_WEBHOOK_SECRET is not -- the webhook "
            "endpoint fails closed (503) without it, so any payment whose client never "
            "returns would be charged and never activate. Set the webhook secret from the "
            "Razorpay dashboard's webhook configuration before running in production."
        )
