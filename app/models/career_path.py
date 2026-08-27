import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CareerPath(Base):
    """A student's job-readiness plan for one target career role. Deliberately independent
    of PlacementPath/Roadmap -- Career Path answers "how do I become job-ready for role X",
    Roadmap answers "what should I work on in my current learning plan"; the two must never
    share tables, services, or progress state (see app/services/career_path/).

    One row per (profile_id, role_family_key) -- re-selecting a role the student has already
    explored finds and refreshes that same row (carrying forward completed skill/project
    status by stable key) rather than creating a new history entry. is_primary tracks which
    one is "the" currently active Career Path (service-enforced single-true-per-profile,
    same style as StudentProfile.custom_target_role's XOR enforcement); the full list of a
    profile's CareerPath rows *is* "recently selected / saved roles" -- no separate history
    table is needed."""

    __tablename__ = "career_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)

    # Role selection is intentionally independent of StudentProfile.target_role_id /
    # custom_target_role -- Career Path never reads or writes those fields.
    role_id: Mapped[Optional[int]] = mapped_column(ForeignKey("roles.id"), nullable=True)
    custom_role: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    # Stable resolved-family slug used for carry-forward and the one-active-per-family
    # invariant. "custom:<slug>" for AI-assisted roles that don't match the curated catalog.
    role_family_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    display_role_name: Mapped[str] = mapped_column(String(200), nullable=False)

    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # catalog | ai_generated | fallback -- which tier of role_catalog/ai_generation produced
    # this path's content, surfaced for transparency (e.g. "estimated" labeling downstream).
    generated_via: Mapped[str] = mapped_column(String(20), nullable=False, default="catalog")

    # A snapshot of RoleFamilyDef's overview fields (short_explanation, responsibilities,
    # core_skills, tools, difficulty, entry_level_expectations, interview_focus,
    # portfolio_expectations) at generation/refresh time. Persisted rather than re-derived on
    # every read: for a curated role this would be cheap to re-derive, but for an AI-assisted
    # or fallback role the definition is NOT persisted anywhere else, so re-deriving it on
    # every GET would mean either re-calling the AI on every screen open (violates the
    # "cache, don't regenerate on open" rule) or losing the exact content the student was
    # shown. Snapshotting once, here, keeps every read free of AI calls and DB re-derivation.
    role_overview: Mapped[dict] = mapped_column(JSON, nullable=False)

    # For the AI-assisted/fallback tier only (role_family_key starting "custom:"): a snapshot
    # of the generated phase_skill_templates + project_templates that produced this path's
    # current skill items/projects. NULL for curated-catalog roles, which never need it (the
    # catalog is a static Python dict, always cheap to re-derive). Persisting this is what
    # makes re-selecting an already-generated custom role safe: without it, re-selecting would
    # call the AI again, which is not guaranteed to return the same skill_keys twice, silently
    # breaking the carry-forward-by-key promise documented in
    # career_path/rules.build_phase_specs and resetting the student's progress on every
    # ordinary re-selection. select_role() reuses this cache; only the explicit refresh()
    # action re-calls the AI and overwrites it.
    generated_definition: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
