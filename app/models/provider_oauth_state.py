import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ProviderOAuthState(Base):
    """One in-flight OAuth authorisation attempt: who started it, for which provider, and the
    PKCE verifier needed to finish it.

    This table is why the OAuth flow can be replay-proof. A signed, self-contained state value
    (a JWT, say) would carry the user binding and an expiry perfectly well, but it cannot be
    made single-use, because nothing anywhere records that it has already been redeemed --
    anyone who captures a callback URL could replay it until it expired. Single use requires
    server-side state, so there is server-side state. `consumed_at` is that record.

    Three things are kept out of the database that a naive implementation would put in:

    The raw state value is never stored -- only `state_hash`, its sha256. The state travels
    through the system browser, a redirect chain, and very likely a proxy access log, so it
    should be treated as something an attacker may hold a copy of. Storing only the hash means
    a dump of this table cannot be used to complete anybody's pending authorisation.

    The authorization code is never stored at all, in any column, at any point. It is exchanged
    for a token and discarded within the same request.

    The PKCE verifier never reaches the Android device. The device only ever opens a URL that
    the backend built and receives a status back; the verifier is generated here, encrypted
    here, and used here. That is what makes the client secret genuinely the only copy, and it
    is also why the redirect back into the app can safely carry nothing but a success flag.

    `code_verifier_encrypted` uses the same envelope as provider tokens (app/core/
    token_crypto.py). The verifier is short-lived and useless on its own, but it is one half of
    a credential exchange, and encrypting it costs one function call.

    Rows are expected to outlive their usefulness by minutes and are swept by expiry; the index
    on `expires_at` is what makes that sweep cheap rather than a full scan of every
    authorisation ever started.

    Every timestamp here is `DateTime(timezone=True)` -- timestamptz -- which deviates from the
    plain `DateTime` used by the other sixty tables in this schema, and the deviation is
    deliberate and measured rather than stylistic.

    The rest of the codebase writes an *aware* UTC value (`datetime.now(timezone.utc)`) into a
    *naive* column. PostgreSQL resolves that by casting timestamptz to timestamp through the
    server's TimeZone setting, so on any server not set to UTC the stored value is local time
    while every reader assumes UTC. Measured directly against PostgreSQL 18 with
    TimeZone=Asia/Calcutta: 2026-09-13 12:00:00+00:00 was written, and 2026-09-13 17:30:00 came
    back -- a silent five-and-a-half hour shift.

    On most columns that is a display and ordering nuisance. On `expires_at` it is a security
    control: an expiry comparison between a stored local time and a computed UTC "now" keeps
    every pending authorisation valid for an extra offset-sized window, which is exactly the
    replay window `consumed_at` and the expiry exist to close. timestamptz stores an instant,
    so the comparison is correct whatever the server is set to.

    Applied to all four of this feature's tables rather than only to this column, so there is
    one rule for the feature instead of a per-column judgement call. The existing tables are
    left alone: changing them is a schema-wide migration with its own backfill question, and it
    is not this phase's work -- it is recorded in the Phase 1 report as a pre-existing finding.
    """

    __tablename__ = "provider_oauth_states"

    id: Mapped[int] = mapped_column(primary_key=True)

    # sha256 hex of the state parameter. Unique because two live states colliding would be
    # precisely the ambiguity an attacker wants: a lookup that could resolve to either of two
    # users' pending authorisations.
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # The binding that makes a stolen callback useless. The callback arrives without a session
    # -- it is a browser redirect from the provider -- so the identity of the student who
    # started the flow can only come from here. It must never be taken from anything the
    # request itself supplies.
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)

    code_verifier_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # Short -- minutes, not hours. A pending authorisation is a window in which a captured
    # state is worth something, and the student is standing in front of the screen for the
    # whole of it.
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Null until redeemed, then set once and never cleared. The callback must treat a non-null
    # value as a hard failure rather than as "already done, carry on" -- a second arrival of
    # the same state is either a replay or a duplicate submission, and neither should be
    # allowed to mint a second connection.
    consumed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
