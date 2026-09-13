from typing import Optional
from urllib.parse import urlparse

from pydantic import field_validator
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
    #   * "jobvetta"  -- India-only coverage, and a hard ceiling of 50 requests per key per
    #                    UTC day shared by the whole deployment. That ceiling is a property of
    #                    the key, not of any student's plan: a premium subscription is
    #                    unlimited Job Hunt, and is still subject to it. See
    #                    app/services/job_hunt/provider_budget.py.
    job_provider: str = "none"
    adzuna_app_id: Optional[str] = None
    adzuna_app_key: Optional[str] = None
    # Adzuna is per-country; "in" is India. Changing this changes which market students see.
    adzuna_country: str = "in"
    careerjet_affid: Optional[str] = None
    careerjet_locale_code: str = "en_IN"
    # Bearer token for https://api.jobvetta.com/v1. Supplied through the environment only --
    # never committed, never logged, never returned to the client, and never given a
    # placeholder default here: a fake default would make an unconfigured deployment look
    # configured and turn "not set up" into a stream of 401s from the provider.
    jobvetta_api_key: Optional[str] = None

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

    # Encrypts provider access tokens and PKCE verifiers at rest (app/core/token_crypto.py).
    # A Fernet key: url-safe base64 of 32 random bytes. Unset is a legitimate state and means
    # "Professional Presence cannot store credentials", so the feature reports itself
    # unavailable instead of offering a Connect button that fails after the student has already
    # authorised on GitHub's site. It is NOT defaulted to anything: a default would be a
    # published encryption key, which is the same defect JWT_SECRET_KEY's `.env.example`
    # placeholder turned out to be. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Rotating it does not lose student data -- stored tokens become undecryptable and those
    # connections need re-authorising, while all derived evidence is refetchable.
    token_encryption_key: Optional[str] = None

    # GitHub OAuth (Professional Presence). Both unset is a legitimate state and means the
    # feature reports itself unavailable rather than offering a Connect button that fails after
    # the student has already authorised on GitHub's site.
    #
    # The client secret exists ONLY here. It is never sent to the Android app, which receives a
    # finished authorization URL from this server and hands back only the authorization code --
    # so the app is never entitled to a secret it could leak by being decompiled.
    github_client_id: Optional[str] = None
    github_client_secret: Optional[str] = None
    # Where GitHub sends the student's browser after they approve. Must match the callback URL
    # registered on the GitHub OAuth App exactly, or GitHub refuses the authorisation with its
    # own error page before our code ever runs.
    #
    # Configured rather than derived from the incoming request's Host header. TrustedHostMiddleware
    # already constrains that header, but building a redirect target out of anything the caller
    # sends is how open redirects start, and this value is also the one thing that has to agree
    # with a setting held on GitHub's side -- so it belongs somewhere a human sets deliberately.
    github_oauth_redirect_uri: Optional[str] = None
    # Where the callback sends the browser once the connection is finished, to hand control back
    # to the Android app. Carries a result only -- never a token, code, state or verifier -- so a
    # hostile app registering the same scheme learns one boolean. See app/api/professional_presence.py.
    professional_presence_app_redirect: str = "aiplacementos://presence/callback"

    @field_validator("database_url")
    @classmethod
    def _normalise_postgres_scheme(cls, value: str) -> str:
        """Accept the `postgres://` URL every managed provider hands out.

        SQLAlchemy dropped that alias in 1.4 and answers it with
        `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres` -- verified against
        the pinned 2.0.52. Render, Heroku and several others still present exactly that form as
        the connection string to copy, so pasting the value the dashboard gives you takes the
        application down at import with an error that names a SQLAlchemy plugin and says nothing
        about the URL being the problem.

        Rewriting the scheme is safe because `postgres://` has no other meaning: it is the same
        database, the same driver and the same credentials, spelled the way the provider spells
        it. Doing it here rather than in the deployment notes means it cannot be forgotten under
        pressure, and it happens before validate_for_production() reads the value, so the
        SQLite guard still sees the real scheme.
        """
        if value.startswith("postgres://"):
            return "postgresql+psycopg2://" + value[len("postgres://"):]
        return value


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


# Every value of JWT_SECRET_KEY that appears somewhere public in this repository.
#
# Checking only against the code default was not enough, and the gap was the *likeliest*
# production mistake rather than an exotic one: `.env.example` ships its own placeholder, so
# copying that file to `.env`, filling in the database URL and deploying produced a signing key
# that is readable by anyone who can see the repo -- and that sailed past a guard comparing
# against a different string. Anyone could then mint a token for any user.
#
# Any new placeholder written into a committed file belongs in this set.
_PUBLISHED_JWT_SECRETS = frozenset(
    {
        "dev-only-insecure-secret-change-in-production",  # Settings default
        "change-me-in-production",  # .env.example
        "changeme",
        "secret",
        "test",
    }
)

# A floor, not a strength test. It cannot tell a good secret from a bad one -- it only catches
# a value nobody could have generated randomly and meant.
_MIN_JWT_SECRET_LENGTH = 32


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
    if settings.jwt_secret_key in _PUBLISHED_JWT_SECRETS or len(settings.jwt_secret_key) < _MIN_JWT_SECRET_LENGTH:
        raise RuntimeError(
            "JWT_SECRET_KEY is a published placeholder or too short to be a real secret -- "
            "set a real, random secret before running in production, e.g.: "
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
    if settings.database_url.startswith("sqlite"):
        # The most destructive default in this file, and the quietest. DATABASE_URL defaults to
        # a local SQLite file, so a deployment where the variable is unset, misspelled, or
        # attached to the wrong environment group does not fail -- it starts perfectly, creates
        # app.db inside the container, and serves real traffic from it.
        #
        # Nothing downstream notices. /health/ready reports "database": "reachable" and
        # "schema": "current", because SQLite genuinely is reachable and alembic genuinely did
        # migrate it. Signups succeed, logins succeed, resumes upload. Then the next deploy
        # replaces the container and every account created since the last one is gone, with no
        # error at any point and no backup to restore from -- the managed PostgreSQL sitting
        # alongside was simply never connected to.
        #
        # Grouped with the storage check below: both are dev defaults that silently destroy
        # production data rather than failing, which is what makes them worth a startup guard
        # at all. This is the more severe of the two -- it loses the accounts themselves.
        raise RuntimeError(
            f"DATABASE_URL is still SQLite ({settings.database_url!r}) -- production data would "
            "be written to a file inside the container and destroyed by the next deploy, while "
            "/health/ready still reports the database as reachable. Set DATABASE_URL to the "
            "PostgreSQL connection string, e.g. "
            "postgresql+psycopg2://USER:PASSWORD@HOST:5432/DBNAME"
        )
    if settings.storage_provider == "local":
        # The same class as the checks above, and the most expensive one to discover late.
        # LocalStorageProvider's own docstring already says it is unsuitable for production --
        # "files vanish on redeploy/container restart and aren't shared across multiple app
        # instances" -- but nothing enforced it, and STORAGE_PROVIDER defaults to "local", so a
        # deployment that simply never set it starts cleanly and accepts uploads for as long as
        # the container lives.
        #
        # What makes it worth failing startup over rather than logging: the loss is silent and
        # one-directional. `save()` mkdirs and writes happily, the Resume row is committed with
        # a key, and the API returns 200. The file is then destroyed by the next deploy while
        # the row survives pointing at it -- so the failure surfaces later, to a student, as a
        # resume that cannot be opened or re-analysed, with the original upload gone for good.
        # Every ATS check, JD match and evidence signal derived from resumes rests on this.
        #
        # STORAGE_PROVIDER=s3 needs no check here: S3StorageProvider raises on construction
        # when its bucket/credentials are missing, which is the "fails loudly on first real
        # use" treatment this function's docstring describes.
        raise RuntimeError(
            "STORAGE_PROVIDER is still 'local' (files are written to the container filesystem) "
            "-- uploaded resumes would be destroyed by the next deploy or restart while their "
            "database rows survive pointing at them. Set STORAGE_PROVIDER=s3 with "
            "STORAGE_S3_BUCKET / STORAGE_S3_REGION / STORAGE_S3_ACCESS_KEY_ID / "
            "STORAGE_S3_SECRET_ACCESS_KEY (any S3-compatible service works; set "
            "STORAGE_S3_ENDPOINT_URL for a non-AWS one) before running in production."
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
    if settings.token_encryption_key:
        # Exactly the Razorpay treatment above, and for the same reason. An UNSET key is left
        # alone: it means Professional Presence is switched off, the feature says so, and
        # token_crypto raises on first real use -- the "fails loudly on first use" category
        # this function's docstring describes. What has no safety net is a key that is *set*
        # but malformed, because nothing reads it until a student has already been sent to
        # GitHub, authorised there, and been redirected back. The failure would land after the
        # irreversible part of the flow, on a screen the student cannot retry their way out of,
        # and it would land on every student at once.
        #
        # Local import: token_crypto imports `settings` from this module, so importing it at
        # module scope would be circular. Same pattern as app/api/deps.py's require_premium.
        from app.core import token_crypto

        if not token_crypto.is_configured():
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY is set but is not a valid Fernet key, so provider "
                "credentials could not be encrypted or read -- every GitHub connection would "
                "fail after the student had already authorised on GitHub. Generate a real key "
                'with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())" (or leave it unset to disable '
                "Professional Presence entirely)."
            )
    if settings.github_client_id:
        # Same shape as the Razorpay webhook-secret check directly above: an unset client id
        # means the feature is off and nothing complains, but a HALF-configured one fails only
        # after a student has been sent to GitHub, approved the consent screen, and been
        # redirected back -- past the point they can retry their way out of, and for everyone at
        # once. Each of the three below is individually sufficient to break the flow at exactly
        # that moment, so each is checked.
        if not settings.github_client_secret:
            raise RuntimeError(
                "GITHUB_CLIENT_ID is configured but GITHUB_CLIENT_SECRET is not -- the "
                "authorization code could never be exchanged, so every student would approve "
                "on GitHub and then land on a failure. Set it from the GitHub OAuth App's "
                "settings page, or unset GITHUB_CLIENT_ID to disable the feature."
            )
        if not settings.github_oauth_redirect_uri:
            raise RuntimeError(
                "GITHUB_CLIENT_ID is configured but GITHUB_OAUTH_REDIRECT_URI is not -- GitHub "
                "rejects an authorization request whose redirect_uri does not match the one "
                "registered on the OAuth App. Set it to this deployment's callback URL, e.g. "
                "https://api.example.com/professional-presence/connections/github/callback"
            )
        if not settings.github_oauth_redirect_uri.startswith("https://"):
            # The authorization code arrives as a query parameter on this URL. Over plain HTTP
            # it is readable by anything on the path, and a code plus a leaked client secret is
            # a complete account connection. GitHub permits http for localhost during
            # development; production has no such excuse.
            raise RuntimeError(
                "GITHUB_OAUTH_REDIRECT_URI is not https -- the authorization code travels as a "
                f"query parameter on that URL ({settings.github_oauth_redirect_uri!r}), so it "
                "would be exposed in transit. Use an https callback URL in production."
            )
        if not settings.token_encryption_key:
            # Reaches further than the feature: without a key the PKCE verifier cannot be
            # stored, so authorisation cannot even begin. Better to say so at boot than to
            # serve a Connect button that raises on first use.
            raise RuntimeError(
                "GITHUB_CLIENT_ID is configured but TOKEN_ENCRYPTION_KEY is not -- GitHub "
                "access tokens and PKCE verifiers could not be encrypted, and storing them in "
                "plaintext is not an accepted fallback. Generate a key with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
