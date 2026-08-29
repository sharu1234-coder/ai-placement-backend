from typing import Optional

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

    # Comma-separated Host header allowlist (TrustedHostMiddleware, app/main.py). "*" (dev
    # default) accepts any Host -- fine locally, but a production deployment should set
    # this to its real domain(s) (e.g. "api.aiplacementos.com") to reject Host-header
    # spoofing/cache-poisoning attempts. TLS itself is terminated at the reverse proxy /
    # deployment platform (nginx, Render, Railway, an AWS ALB, ...), not by this app --
    # see README "HTTPS / network security" for the full picture, including why Uvicorn
    # needs --proxy-headers when run behind one of those.
    allowed_hosts: str = "*"


settings = Settings()


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
