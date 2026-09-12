import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CareerStrategyTarget(Base):
    """One role a student wants their strategy compared against.

    Deliberately not a second "target role" concept. The student's *actual* target is still
    whatever role_context resolves -- their primary Career Path, falling back to
    StudentProfile.target_role_id -- and nothing here changes that. These rows only say "also
    show me what my strategy would look like for this role", which is a question the app could
    not previously answer without the student destructively switching their target and losing
    their real one.

    A row is therefore not a commitment and carries no state of its own: no readiness, no
    bottleneck, no snapshot. Every one of those is recomputed by the same deterministic engine
    the primary target uses, with the role context swapped. Storing any of it here would create
    exactly the second strategy engine this feature exists to avoid.

    Kept minimal on purpose -- (profile, role) and when it was added. The unique constraint is
    what makes "add this target" idempotent: asking twice is one row, not two, and the database
    is what guarantees that rather than a check-then-insert in Python.
    """

    __tablename__ = "career_strategy_targets"
    __table_args__ = (
        UniqueConstraint("profile_id", "role_id", name="uq_career_strategy_target_per_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )
    # Restricted to the catalogue's Role rows rather than a free-text name: a target has to
    # resolve to real skill weights or there is nothing to measure against, and a typo would
    # otherwise produce a confident-looking strategy for a role that does not exist.
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False, index=True)
    # Ordering key for the comparison, so two targets added in a known order are always
    # presented in that order rather than in whatever sequence the database returns.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
