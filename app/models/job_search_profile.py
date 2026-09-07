import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class JobSearchProfile(Base):
    """The transparent, student-editable inputs the Job Hunt matching engine runs on.

    Deliberately a separate table from StudentProfile rather than more columns on it: these
    are Job-Hunt-specific search *preferences* with their own provenance tracking, and a
    student who never opens Job Hunt should never have a half-populated set of them. One row
    per profile (see the unique constraint) -- this is a single editable document, not a
    history.

    Why provenance is a first-class column rather than something inferred: the product rule
    (spec section 3) is that every signal must be attributable to explicit user input, resume
    extraction, verified evidence, or inference. The UI shows the student where each value
    came from and lets them correct it, which is only possible if we actually recorded it at
    write time. Shape is {"target_role_title": "explicit", "normalized_skills": "resume", ...}.
    """

    __tablename__ = "job_search_profiles"
    __table_args__ = (UniqueConstraint("profile_id", name="uq_job_search_profile_per_profile"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )

    # Which resume the student chose to hunt with. Nullable because the role-only flow is a
    # first-class path (spec section 4), not a degraded one -- never defaulted to "the latest
    # resume" behind the student's back.
    resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resumes.id"), nullable=True)

    # Structured role link when the student picked a catalog role, plus the free-text title
    # they actually typed. Both are kept: the id drives role-family skill weights, the raw
    # string is what we echo back to them (spec section 3: "retain original user wording").
    target_role_id: Mapped[Optional[int]] = mapped_column(ForeignKey("roles.id"), nullable=True)
    target_role_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Normalized skill keys used for matching. The student's original wording survives in
    # `declared_skills`; this is the canonicalized form (see job_hunt/normalization.py).
    normalized_skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    declared_skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # -- Preferences. Every one is nullable and means "unknown", never a default that would
    # silently filter the student's results (spec section 6: unknown must stay unknown). ----
    preferred_location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    work_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # remote/hybrid/onsite
    experience_level: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    employment_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    salary_min_expected: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    preferred_companies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    open_to_relocation: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    signal_provenance: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
