import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ExternalProfileSnapshot(Base):
    """The normalised state of a student's external profile, recorded only when it changes.

    This exists to answer "what has actually improved since last month?" without that question
    turning into a scroll through identical rows. `fingerprint` hashes the material content of
    the snapshot; the service compares it against the most recent row for the connection and
    writes nothing when they match. Opening the Professional Presence screen therefore costs no
    rows at all -- only a real change to the student's GitHub does.

    Deliberately NOT a second source of truth. Live evidence still comes from
    external_evidence_items through the Evidence Engine on every read, exactly as today's
    readiness is computed live rather than cached. What lives here is only what history needs:
    a past state to diff against, which by definition cannot be recomputed later because the
    provider no longer reports it. The same narrowly-scoped exception readiness_snapshots and
    evidence_state_snapshots already are.

    `sequence` rather than ordering by `created_at`, following placement_strategy_snapshots for
    the same reason it did: two concurrent syncs can produce identical wall-clock timestamps,
    and a clock adjustment can make a later row look earlier. The unique constraint turns the
    ordering into something the database enforces, and turns a concurrent double-write into an
    IntegrityError the service can recover from instead of two adjacent duplicate rows.

    Note this is ordering per *connection*, not per profile. A student who disconnects GitHub
    and reconnects gets a new connection row, and their new history starts at 1 -- which is
    correct: the old grant's snapshots describe an authorisation that no longer exists, and
    presenting the two as one continuous series would imply a continuity that was broken.

    There is no separate "captured at" column. The snapshot is written in the same operation
    that fetches it, so created_at and any capture time would differ by milliseconds and invite
    a reader to believe a distinction exists that does not.
    """

    __tablename__ = "external_profile_snapshots"
    __table_args__ = (
        UniqueConstraint("connection_id", "sequence", name="uq_external_profile_snapshot_sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("provider_connections.id"), nullable=False, index=True)

    # 1-based, contiguous per connection. See the class docstring for why this exists alongside
    # created_at.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    # sha256 hex of the material content of `payload`. Indexed because the write path's first
    # question is always "is this the same as what we already have?".
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # The normalised snapshot -- repository quality summary, language distribution, profile
    # completeness flags. Never the provider's raw response: see the same reasoning on
    # external_evidence_items.detail. Stored whole rather than as a fixed set of columns
    # because comparing two snapshots has to be able to say *what* changed, and a fixed set of
    # columns only ever answers the questions someone thought of on the day it was written.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
