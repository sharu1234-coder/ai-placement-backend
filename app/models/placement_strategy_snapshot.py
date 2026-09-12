import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PlacementStrategySnapshot(Base):
    """One recorded strategic decision for one student, written only when the decision changes.

    The screen is read far more often than the strategy actually moves. Writing a row per read
    would turn "what changed since last week?" into a scroll through a hundred identical rows,
    and would make the honest answer -- *nothing changed* -- indistinguishable from noise. So
    `decision_fingerprint` is a hash of the material decision (state, the primary bottleneck's
    identity and bands, the queue in order, the deferred list) and a snapshot is written only
    when that hash differs from the most recent one. Wording, timestamps and occurrence counts
    are deliberately outside the fingerprint: a weakness being seen a fourth time is real, but
    it is not a change of *strategy*, and it should not create a history entry claiming one.

    `sequence` is a per-profile counter rather than an ordering by `created_at`. Two concurrent
    reads can produce the same wall-clock timestamp, and clock adjustments can make a later row
    look earlier; the unique constraint on (profile_id, sequence) makes the ordering a fact the
    database enforces instead of an assumption, and turns a concurrent double-write into an
    IntegrityError the service can recover from (see career_strategy/snapshots.py) rather than
    into two adjacent duplicate rows.

    `decision` holds the full serialised StrategyDecision, not a summary. Comparing two
    snapshots has to be able to say *what* was in the queue and *why*, and a summary would only
    ever support the questions someone thought of today.

    `strategy_version` is stored per row because the ranking rules can change. A decision made
    under v1 must never be silently reinterpreted as though today's rules produced it -- a
    comparison that spans a version bump says so out loud.

    Named `placement_strategy_snapshots` while the code around it is `career_strategy`: the
    table name is fixed by spec and renaming a persisted table is not worth a migration.
    """

    __tablename__ = "placement_strategy_snapshots"
    __table_args__ = (
        UniqueConstraint("profile_id", "sequence", name="uq_placement_strategy_snapshot_sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )
    # 1-based, contiguous per profile. See the class docstring for why this exists alongside
    # created_at.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)

    # Denormalised from `decision` so history lists and "did the bottleneck change?" checks do
    # not have to parse every stored payload. Null when the decision had no primary bottleneck
    # at all (no target role, or not enough data) -- never a placeholder.
    primary_bottleneck_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    primary_category: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    primary_subject: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    decision_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision: Mapped[dict] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
