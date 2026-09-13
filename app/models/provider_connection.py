import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

# Provider keys. Strings rather than a database ENUM: adding a provider (Kaggle, LeetCode, a
# portfolio site) is meant to be an adapter plus a row value, and a Postgres ENUM would turn
# each one into a migration with an ALTER TYPE that cannot run inside a transaction on older
# servers. The application validates against this tuple; the column just stores the key.
PROVIDER_GITHUB = "github"
PROVIDER_LINKEDIN = "linkedin"
PROVIDERS = (PROVIDER_GITHUB, PROVIDER_LINKEDIN)

# Connection lifecycle. `needs_reauth` is the one that earns its place: a token the provider
# has started rejecting (401) is NOT the same as a connection the student revoked, and the two
# need different words on screen -- "reconnect GitHub" versus "connect GitHub". Without it both
# collapse into "not connected" and the student is told to do something they already did.
STATUS_ACTIVE = "active"
STATUS_NEEDS_REAUTH = "needs_reauth"
STATUS_REVOKED = "revoked"
CONNECTION_STATUSES = (STATUS_ACTIVE, STATUS_NEEDS_REAUTH, STATUS_REVOKED)

# Synchronisation outcome, kept separate from `status` on purpose. A connection can be
# perfectly valid and still have never synced, or have failed its last sync because GitHub was
# down. Collapsing these into one column is what produces the failure this feature must not
# have: "provider unavailable" being rendered to the student as "you have no evidence".
SYNC_NEVER = "never"
SYNC_PENDING = "pending"
SYNC_OK = "ok"
SYNC_FAILED = "failed"
SYNC_STATUSES = (SYNC_NEVER, SYNC_PENDING, SYNC_OK, SYNC_FAILED)

# Error CATEGORIES, never provider error text. The distinction is the point of the column: a
# provider message can echo a request URL, a repository name, or a token fragment back at us,
# and this row is read by support tooling and serialised into API responses. A fixed vocabulary
# cannot leak anything, and it is also what the UI actually branches on.
ERROR_RATE_LIMIT = "rate_limit"
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_FORBIDDEN = "forbidden"
ERROR_NOT_FOUND = "not_found"
ERROR_UNAVAILABLE = "unavailable"
ERROR_TIMEOUT = "timeout"
ERROR_MALFORMED = "malformed"
ERROR_KINDS = (
    ERROR_RATE_LIMIT,
    ERROR_UNAUTHORIZED,
    ERROR_FORBIDDEN,
    ERROR_NOT_FOUND,
    ERROR_UNAVAILABLE,
    ERROR_TIMEOUT,
    ERROR_MALFORMED,
)


class ProviderConnection(Base):
    """One student's authorised link to one external provider (GitHub, LinkedIn).

    Identity is `provider_user_id` -- GitHub's numeric account id -- and never the username.
    A GitHub login can be renamed by its owner at any time, and once renamed the old login
    becomes available for someone else to register. Keying on it would mean a connection
    silently following whoever holds the name today, which is an account-takeover shaped bug
    rather than a cosmetic one. `provider_username` exists only so the UI can say "connected as
    @octocat", is refreshed from the provider, and is never read to decide who owns anything.

    Credentials are stored encrypted (app/core/token_crypto.py) and are never serialised into
    an API response -- the response schemas in this feature do not contain a token field at
    all, which is a stronger guarantee than remembering to exclude one.

    Reconnecting after a revoke inserts a NEW row rather than reviving the old one. That keeps
    `revoked_at` meaning "this specific grant ended at this time" instead of being overwritten,
    and it is why both uniqueness rules below are partial: any number of historical revoked
    rows may exist, but only one live connection per (profile, provider) and only one live
    connection per external identity.

    The second of those is the one that is easy to miss. Without it, two students could each
    connect the same GitHub account -- one of them borrowing another person's repositories as
    evidence for their own career readiness. It is a partial index rather than a plain unique
    constraint precisely so that giving an account up (revoking) genuinely releases it.

    Both indexes carry `sqlite_where` alongside `postgresql_where`. SQLite has supported
    partial indexes since 3.8.0, and without the dialect-specific clause SQLAlchemy would emit
    a *full* unique index on the development database -- which would reject a legitimate
    reconnect-after-revoke locally while production accepted it. The PostgreSQL behaviour is
    still tested explicitly (tests/unit/test_provider_connection_constraints.py); this pair
    exists so the two databases agree, not as a substitute for testing the real one.
    """

    __tablename__ = "provider_connections"
    __table_args__ = (
        Index(
            "uq_provider_connection_active_per_provider",
            "profile_id",
            "provider",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
        Index(
            "uq_provider_connection_active_identity",
            "provider",
            "provider_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)

    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    # The provider's own immutable account identifier, stored as text because providers do not
    # agree on its shape: GitHub's is a 64-bit integer, LinkedIn's OpenID `sub` is an opaque
    # string. Storing the integer as text costs nothing and keeps one column for both.
    provider_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Display only. See the class docstring: never an identity key, and safe to be stale.
    provider_username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    # Encrypted at rest. Nullable because a connection row legitimately outlives its credential:
    # on disconnect the token is destroyed here while the row survives as the audit record of a
    # grant that once existed.
    access_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "where applicable" -- GitHub OAuth App user tokens do not expire and carry no refresh
    # token, so this stays null for every GitHub row. LinkedIn issues 60-day tokens, and only
    # select partners get programmatic refresh, so it will usually be null there too. The column
    # exists so that a provider which does refresh needs no migration, not because V1 uses it.
    refresh_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # What the provider actually granted, which is not necessarily what was asked for. Stored
    # so a later feature needing a scope we do not hold can say "reconnect to allow X" instead
    # of failing at call time. An empty string is a real, meaningful value for GitHub: the
    # minimum-permission choice for V1 requests no scope at all, and empty scope already grants
    # read-only access to public profile and repository information.
    granted_scopes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_ACTIVE)
    sync_status: Mapped[str] = mapped_column(String(20), nullable=False, default=SYNC_NEVER)
    # Last SUCCESSFUL sync, not last attempt. A failed attempt must not advance this, or
    # "last updated 2 minutes ago" would be shown next to data that is actually weeks old.
    last_synced_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # One of ERROR_KINDS. Deliberately not a message -- see the constant block above.
    last_error_kind: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    # Set when the student disconnects or the grant is withdrawn. Also the predicate both
    # partial unique indexes are built on, so it is the single fact that decides whether this
    # row occupies the live slot for its (profile, provider) and for its external identity.
    revoked_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # No separate `connected_at`: a row is only ever created on a successful connection, and a
    # reconnect creates a new row rather than reusing this one, so created_at already IS the
    # moment this grant began.
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
