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
    monkeypatch.setattr(settings, "storage_provider", "s3")
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.example.com:5432/app")
    # This dev checkout's .env ships a real rzp_test_ key (needed for the Razorpay-flow
    # tests elsewhere) -- explicitly override it here so this "everything else is production-
    # ready" test isn't coupled to that unrelated local dev value.
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_abc123")
    # Required alongside a configured key since the webhook-secret check was added: without
    # it the webhook endpoint fails closed and paid users would never be activated.
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "a-real-webhook-secret")

    validate_for_production()  # must not raise


def test_validate_for_production_rejects_the_env_example_placeholder(monkeypatch) -> None:
    """The likeliest real-world mistake, and the one the original guard let through.

    `.env.example` ships JWT_SECRET_KEY=change-me-in-production. Copying that file to `.env`,
    filling in the database URL and deploying is the obvious path -- and it used to pass,
    because the guard only compared against the *code* default, a different string. The result
    was a signing key published in the repository: anyone who could read it could forge a token
    for any user.
    """
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "change-me-in-production")
    with pytest.raises(RuntimeError, match="placeholder or too short"):
        validate_for_production()


@pytest.mark.parametrize("weak", ["changeme", "secret", "test", "short"])
def test_validate_for_production_rejects_obvious_placeholders(monkeypatch, weak) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", weak)
    with pytest.raises(RuntimeError):
        validate_for_production()


def test_validate_for_production_rejects_a_secret_too_short_to_be_random(monkeypatch) -> None:
    """A floor, not a strength test: it cannot tell a good secret from a bad one, only catch a
    value nobody could have generated randomly and meant."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "a" * 31)
    with pytest.raises(RuntimeError, match="placeholder or too short"):
        validate_for_production()


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
    monkeypatch.setattr(settings, "storage_provider", "s3")
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.example.com:5432/app")
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
    monkeypatch.setattr(settings, "storage_provider", "s3")
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.example.com:5432/app")
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
    monkeypatch.setattr(settings, "storage_provider", "s3")
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.example.com:5432/app")
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
    # DATABASE_URL defaults to a SQLite file, which the guard now rejects in production.
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.example.com:5432/app")
    # STORAGE_PROVIDER defaults to "local", which the guard now rejects in production -- the
    # same treatment _production_ready_ai gives AI_PROVIDER, and for the same reason: tests
    # about other settings should not be coupled to this unrelated default.
    monkeypatch.setattr(settings, "storage_provider", "s3")
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
    monkeypatch.setattr(settings, "storage_provider", "s3")
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.example.com:5432/app")
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
    monkeypatch.setattr(settings, "storage_provider", "s3")
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.example.com:5432/app")
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", None)

    validate_for_production()  # must not raise


def test_validate_for_production_rejects_local_file_storage(monkeypatch) -> None:
    """The costliest default to discover late, and the one nothing else complains about.

    LocalStorageProvider's own docstring already called itself unsuitable for production --
    "files vanish on redeploy/container restart" -- but STORAGE_PROVIDER defaults to "local"
    and nothing enforced it. A deployment that never set it starts cleanly and keeps accepting
    uploads for as long as the container happens to live.

    The loss is silent and one-directional, which is what makes it worth failing startup over.
    `save()` creates the directory, writes the file and returns; the Resume row commits with a
    key and the API answers 200. The next deploy destroys the file while the row survives
    pointing at it -- so it surfaces later, to a student, as a resume that can no longer be
    opened or re-analysed, with the original upload gone for good. ATS checks, JD matching and
    every resume-derived evidence signal rest on that file still being there.
    """
    _production_base(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "storage_provider", "local")

    with pytest.raises(RuntimeError, match="STORAGE_PROVIDER"):
        validate_for_production()


def test_local_file_storage_is_fine_in_development(monkeypatch) -> None:
    """Every dev checkout runs exactly this and must not be blocked."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "storage_provider", "local")

    validate_for_production()  # must not raise


def test_validate_for_production_accepts_s3_storage(monkeypatch) -> None:
    """S3StorageProvider raises on construction when its bucket/credentials are missing, so
    the guard deliberately does not re-check them here -- that is the "fails loudly on first
    real use" treatment this function's docstring describes, and a second copy of the rule
    would be free to disagree with the first."""
    _production_base(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "storage_provider", "s3")

    validate_for_production()  # must not raise


def test_validate_for_production_rejects_a_sqlite_database_url(monkeypatch) -> None:
    """The most destructive default in the file, and the quietest.

    DATABASE_URL defaults to a local SQLite file. A deployment where the variable is unset,
    misspelled, or attached to the wrong environment group therefore does not fail -- it starts
    perfectly, creates app.db inside the container, and serves real traffic from it.

    Nothing downstream notices, which is what makes a startup guard the only place this can be
    caught. `/health/ready` reports the database as reachable and the schema as current, because
    SQLite genuinely is reachable and alembic genuinely did migrate it. Signups, logins and
    resume uploads all succeed. The next deploy then replaces the container, and every account
    created since the previous one is gone -- with no error at any point, no backup to restore
    from, and a managed PostgreSQL sitting alongside that was simply never connected to.
    """
    _production_base(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "database_url", "sqlite:///./app.db")

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        validate_for_production()


def test_a_sqlite_database_url_is_fine_in_development(monkeypatch) -> None:
    """The default dev checkout and the whole test suite run on exactly this."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "database_url", "sqlite:///./app.db")

    validate_for_production()  # must not raise


def test_validate_for_production_accepts_a_postgresql_database_url(monkeypatch) -> None:
    _production_base(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.example.com:5432/app")

    validate_for_production()  # must not raise


def test_the_sqlite_check_does_not_depend_on_the_file_path(monkeypatch) -> None:
    """An absolute path, a named file or a bare :memory: URL are all the same mistake -- the
    guard keys on the driver, not on what comes after it."""
    _production_base(monkeypatch)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    for url in ("sqlite:///:memory:", "sqlite:////var/data/app.db", "sqlite+pysqlite:///./x.db"):
        monkeypatch.setattr(settings, "database_url", url)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            validate_for_production()


class TestDatabaseUrlSchemeNormalisation:
    """`postgres://` is the exact string Render's dashboard offers as the connection URL, and
    SQLAlchemy has refused it since 1.4 -- `NoSuchModuleError: Can't load plugin:
    sqlalchemy.dialects:postgres`, an error that names a plugin and never mentions the URL.

    Pasting the value the provider gives you is the obvious thing to do, so the obvious thing
    has to work. These pin that it does, and that nothing else was quietly rewritten with it.
    """

    def test_a_render_style_postgres_url_is_accepted(self) -> None:
        s = Settings(database_url="postgres://u:p@dpg-abc.oregon-postgres.render.com:5432/app")
        assert s.database_url == "postgresql+psycopg2://u:p@dpg-abc.oregon-postgres.render.com:5432/app"

    def test_the_rewritten_url_is_one_sqlalchemy_can_actually_open(self) -> None:
        """The whole point -- asserting on the string alone would pass even if the replacement
        produced something SQLAlchemy still refused."""
        from sqlalchemy import create_engine

        s = Settings(database_url="postgres://u:p@h:5432/db")
        engine = create_engine(s.database_url)  # no connection is made until first use
        assert engine.dialect.name == "postgresql"

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@h:5432/db",
            "postgresql+psycopg2://u:p@h:5432/db",
            "sqlite:///./app.db",
        ],
    )
    def test_every_other_scheme_is_left_exactly_as_it_was(self, url) -> None:
        assert Settings(database_url=url).database_url == url

    def test_normalisation_does_not_smuggle_sqlite_past_the_production_guard(self, monkeypatch) -> None:
        """A rewrite that accidentally matched more than it should could turn the SQLite guard
        into a no-op. It stays a guard."""
        _production_base(monkeypatch)
        monkeypatch.setattr(settings, "ai_provider", "anthropic")
        monkeypatch.setattr(settings, "database_url", Settings(database_url="sqlite:///./app.db").database_url)

        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            validate_for_production()


class TestTokenEncryptionKeyGuard:
    """TOKEN_ENCRYPTION_KEY gets the Razorpay treatment, not the JWT treatment, and the
    difference is deliberate.

    An UNSET key is a legitimate production state: it means Professional Presence is switched
    off, and token_crypto raises on first real use -- the "fails loudly on first use" category
    this module's own docstring describes for ANTHROPIC_API_KEY and the SMTP settings. Refusing
    to boot over it would make an optional feature mandatory.

    A key that is SET but malformed has no such safety net. Nothing reads it until a student
    has already been sent to GitHub, authorised there, and been redirected back -- so the
    failure would land after the irreversible part of the flow, on every student at once, on a
    screen they cannot retry their way out of.
    """

    def test_an_unset_key_does_not_block_startup(self, monkeypatch) -> None:
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        monkeypatch.setattr(settings, "token_encryption_key", None)

        validate_for_production()  # must not raise

    def test_an_empty_key_does_not_block_startup(self, monkeypatch) -> None:
        """`.env.example` ships the setting with no value, so a copied file yields "" rather
        than None. It must mean the same thing as unset, not "set to an invalid key"."""
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        monkeypatch.setattr(settings, "token_encryption_key", "")

        validate_for_production()  # must not raise

    def test_a_malformed_key_is_refused(self, monkeypatch) -> None:
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        monkeypatch.setattr(settings, "token_encryption_key", "not-a-valid-fernet-key")

        with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
            validate_for_production()

    def test_a_real_key_passes(self, monkeypatch) -> None:
        from cryptography.fernet import Fernet

        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())

        validate_for_production()  # must not raise

    def test_the_error_does_not_echo_the_key(self, monkeypatch) -> None:
        """A startup error is printed to logs and pasted into issue trackers. If the key were
        merely mistyped rather than fake, echoing it would publish a real secret."""
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        secret_looking = "aaaabbbbccccddddeeeeffffgggghhhh-but-not-valid-base64-fernet"
        monkeypatch.setattr(settings, "token_encryption_key", secret_looking)

        with pytest.raises(RuntimeError) as caught:
            validate_for_production()
        assert secret_looking not in str(caught.value)


class TestGitHubOAuthGuards:
    """GitHub OAuth gets the Razorpay treatment: unset is off, half-configured is refused.

    The reason half-configured earns a startup failure rather than a runtime one is specific and
    worth stating. Nothing reads these settings until a student has already pressed Connect, been
    sent to GitHub, approved the consent screen, and been redirected back. A missing secret or
    redirect URI surfaces only at that point -- past the part of the flow they cannot retry their
    way out of, and for every student at once. Startup is the last moment the failure is cheap.
    """

    @staticmethod
    def _github(monkeypatch, **overrides):
        from cryptography.fernet import Fernet

        values = {
            "github_client_id": "Iv1.abc123",
            "github_client_secret": "a-real-looking-client-secret",
            "github_oauth_redirect_uri": (
                "https://api.example.com/professional-presence/connections/github/callback"
            ),
            "token_encryption_key": Fernet.generate_key().decode(),
        }
        values.update(overrides)
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    def test_an_unset_client_id_does_not_block_startup(self, monkeypatch) -> None:
        """The feature being switched off is a legitimate production state, and making an
        optional feature mandatory at boot would be the wrong trade."""
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        self._github(monkeypatch, github_client_id=None, github_client_secret=None)

        validate_for_production()  # must not raise

    def test_a_fully_configured_provider_passes(self, monkeypatch) -> None:
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        self._github(monkeypatch)

        validate_for_production()  # must not raise

    def test_a_client_id_without_a_secret_is_refused(self, monkeypatch) -> None:
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        self._github(monkeypatch, github_client_secret=None)

        with pytest.raises(RuntimeError, match="GITHUB_CLIENT_SECRET"):
            validate_for_production()

    def test_a_client_id_without_a_redirect_uri_is_refused(self, monkeypatch) -> None:
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        self._github(monkeypatch, github_oauth_redirect_uri=None)

        with pytest.raises(RuntimeError, match="GITHUB_OAUTH_REDIRECT_URI"):
            validate_for_production()

    def test_a_plaintext_redirect_uri_is_refused(self, monkeypatch) -> None:
        """The authorization code arrives as a query parameter on this URL. Over plain HTTP it is
        readable by anything on the path, and a code plus a leaked client secret is a complete
        account connection."""
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        self._github(
            monkeypatch,
            github_oauth_redirect_uri="http://api.example.com/professional-presence/connections/github/callback",
        )

        with pytest.raises(RuntimeError, match="https"):
            validate_for_production()

    def test_a_client_id_without_an_encryption_key_is_refused(self, monkeypatch) -> None:
        """Reaches further than the token: without a key the PKCE verifier cannot be stored, so
        authorisation could not even begin."""
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        self._github(monkeypatch, token_encryption_key=None)

        with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
            validate_for_production()

    def test_no_guard_message_echoes_the_client_secret(self, monkeypatch) -> None:
        """Startup errors are printed to logs and pasted into issue trackers. A guard that
        quoted the secret back would publish it every time something else was misconfigured."""
        _production_base(monkeypatch)
        _production_ready_ai(monkeypatch)
        secret = "ghs_averyrealisticlookingclientsecretvalue"
        self._github(monkeypatch, github_client_secret=secret, github_oauth_redirect_uri=None)

        with pytest.raises(RuntimeError) as caught:
            validate_for_production()
        assert secret not in str(caught.value)
