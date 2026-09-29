import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class JobListing(Base):
    """One real job advert -- either exactly as a provider returned it, or as a student typed
    it in -- normalized into this app's own shape.

    Provider rows are not owned by any profile. A provider listing is public content that many
    students can match against, so ownership lives on the rows that *reference* it
    (job_listing_matches, saved_jobs) rather than here. That split is what makes account
    deletion correct: deleting a student removes their matches and saves, and never touches a
    listing another student may still be looking at.

    Manually entered rows are the exception, and `created_by_profile_id` is what marks them.
    A job a student found through campus placement or a referral is not public content: its
    title, company and pasted description are that student's own text. Those rows are owned,
    read-guarded, and deleted with the account. The two kinds share this table because
    everything downstream of a listing -- saving, matching, the tracker, the Career Goal link,
    the outcome -- is identical for both, and a second table would have doubled every one of
    those paths to say the same thing twice.

    Every field except the identity trio is Optional on purpose. Providers differ wildly in
    what they populate -- Careerjet frequently omits salary, Adzuna omits work mode entirely --
    and the product rule (spec section 6) is that unknown must remain unknown rather than
    becoming a false match. A NULL here means "the provider did not tell us", and the matching
    engine treats it as an unknown factor, never as a zero or a mismatch.
    """

    __tablename__ = "job_listings"
    __table_args__ = (
        # The provider's own id is the primary dedupe key (spec section 4: "deduplicate using
        # provider IDs first"). Unique per provider rather than globally, since two providers
        # can legitimately use the same opaque id for different adverts.
        UniqueConstraint("provider", "provider_job_id", name="uq_job_listing_provider_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    provider: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    provider_job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Where the student is sent to actually apply. Always the provider's canonical redirect or
    # a link the student typed in themselves, never a URL this app synthesizes -- validated
    # against an allowlist scheme before it is ever handed to the client (see
    # job_hunt/normalization.py::safe_external_url).
    #
    # Nullable since Phase 4, and only ever NULL on a manually entered listing. A provider
    # advert without a link is dropped at ingestion (there is nothing to apply to), but a job
    # a student found through campus placement, a referral, WhatsApp or email genuinely has no
    # URL, and forcing one would mean either refusing those jobs or inventing a link. NULL is
    # the honest record of "there is no page to send them to"; the client hides the apply
    # button rather than opening nothing.
    url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    work_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    employment_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    seniority: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    salary_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    salary_period: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # year/month/hour
    # True when the provider itself says the figure is modelled rather than advertised
    # (Adzuna's salary_is_predicted). Surfaced to the student as an estimate label -- showing
    # a predicted salary as if the employer stated it would be fabricated data.
    salary_is_predicted: Mapped[bool] = mapped_column(nullable=False, default=False)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # When the employer posted it, per the provider. NULL means the provider did not say --
    # the UI must then omit freshness rather than showing "just now" (spec section 12:
    # "posted date only when provider-backed").
    posted_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    # Who typed this listing in, for the manually entered ones. NULL means shared provider
    # content, which is every row written before Phase 4 and every row a search still writes.
    #
    # This is the only ownership this table has, and it exists because a manual listing is the
    # student's own text -- a job description they pasted, a company they named -- while a
    # provider advert is public content many students legitimately see. The read path enforces
    # exactly that asymmetry (job_hunt/service.py::assert_listing_visible): NULL is readable by
    # anyone, a set value by that profile only.
    #
    # A real foreign key rather than a bare integer so that account deletion is forced to
    # account for it: tests/unit/test_account_service.py interrogates the mappers for
    # student_profiles references and fails if delete_account does not mention the model.
    created_by_profile_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=True, index=True
    )

    fetched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    # After this instant the cached copy must not be presented as current. Enforced in
    # job_hunt/service.py, and set from the provider adapter's own retention rule so a
    # provider with stricter caching terms can shorten it without touching call sites.
    expires_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
