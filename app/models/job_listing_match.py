import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class JobListingMatch(Base):
    """One student's explainable score against one job listing.

    Deliberately NOT the existing JobMatch model (app/models/job_match.py). That one belongs
    to the JD Analyzer: it scores a student against a single pasted job *description* they
    supplied, and carries only matching_skills/missing_skills/match_score. This one scores
    against a provider-sourced *listing*, needs component-level breakdown, a score version and
    a calculated_at for staleness, and is produced in bulk by a search. Overloading one table
    for both would have made every column nullable for one of the two callers, and coupled two
    features that must be free to evolve apart.

    score_version is what makes the score reproducible (spec section 6). Weights live
    server-side in job_hunt/matching.py; when they change, the version string changes, and a
    stored score that no longer matches the current version is recomputed rather than shown as
    though it came from today's engine.
    """

    __tablename__ = "job_listing_matches"
    __table_args__ = (
        UniqueConstraint("profile_id", "listing_id", name="uq_job_listing_match_per_profile"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )
    listing_id: Mapped[int] = mapped_column(ForeignKey("job_listings.id"), nullable=False, index=True)

    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    score_version: Mapped[str] = mapped_column(String(20), nullable=False)
    # {"role_alignment": 0.8, "skill_overlap": 0.5, ...} -- the per-factor breakdown behind
    # total_score, so "Why this matches you" is read from the same numbers that produced the
    # headline figure rather than re-derived (and possibly disagreeing) in the UI.
    component_scores: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    matched_skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    missing_skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Factors the provider gave us nothing to judge on (e.g. no salary, no work mode). Shown
    # to the student verbatim so a mid-range score reads as "we could not tell" rather than
    # "you scored badly" -- the honesty rule from spec section 6.
    unknown_factors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")

    calculated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
