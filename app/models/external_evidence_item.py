import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

# What kind of observation produced this row. The vocabulary is small on purpose, and the
# distinction between the first two and the third is the one that matters most:
#
#   repo_primary_language / repo_language
#       GitHub's own analysis of the bytes of code actually in the repository. The student did
#       not type this anywhere -- it is measured from what they wrote. This is genuine external
#       evidence.
#
#   repo_topic
#       A label the repository owner typed onto their own repository. It is hosted externally,
#       but it is still the student asserting something about themselves: anyone can add the
#       topic "kafka" to an empty repository in five seconds.
#
# Both are recorded, because a topic is real context and discarding it would lose information.
# But they must not be weighed the same, and keeping the difference in `signal_kind` means the
# Evidence Engine matcher can decide that in one place instead of every reader guessing. There
# is deliberately no README-text signal in V1: mining prose for skill names is exactly the
# "invent skills from arbitrary filenames" failure the specification rules out.
SIGNAL_REPO_PRIMARY_LANGUAGE = "repo_primary_language"
SIGNAL_REPO_LANGUAGE = "repo_language"
SIGNAL_REPO_TOPIC = "repo_topic"
SIGNAL_KINDS = (SIGNAL_REPO_PRIMARY_LANGUAGE, SIGNAL_REPO_LANGUAGE, SIGNAL_REPO_TOPIC)


class ExternalEvidenceItem(Base):
    """One normalised, provider-observed signal that a student has some skill.

    This table is the entire contract between the provider adapters and the existing Evidence
    Engine. Adapters write rows shaped like this; the Engine reads them through one matcher.
    Nothing provider-specific travels any further into the intelligence layer, which is what
    keeps Career Strategy free of `if github:` branches.

    Three columns that are conspicuously absent, each on purpose:

    `profile_id` -- ownership is already established by `connection_id`, and a provider
    connection cannot change hands. Duplicating the owner would create a second thing that can
    disagree with the first, and the only reason to want it is to save a join the Evidence
    Engine already performs once per profile rather than once per skill.

    `score` -- GitHub does not produce one. A repository is not a graded assessment, and there
    is no honest number to put here. Inventing one (stars, commit counts, a "quality" formula)
    would be a fabricated measurement that the ladder in evidence_engine would then treat as
    real: `verified` requires a source scored at or above 75, so a made-up score would
    manufacture verified skills out of public repositories. Leaving the column out makes that
    impossible rather than merely discouraged.

    `self_asserted` -- every row in this table is, by definition, something observed on an
    external platform rather than something the student told us directly, so the value would be
    False in every row forever. A column that is always False is not a fact, it is an
    opportunity for a future bug to set it True. The matcher hardcodes `self_asserted=False`
    instead. Student-entered LinkedIn information is NOT stored here for exactly this reason --
    it is a claim, not an observation, and it does not belong in a table of observations.

    The unique constraint makes re-synchronisation idempotent. A sync that runs twice observes
    the same language on the same repository both times; without it the student's evidence
    would multiply on every refresh, and breadth in the Evidence Engine is counted in distinct
    sources -- so duplicate rows would eventually inflate confidence on their own.
    """

    __tablename__ = "external_evidence_items"
    __table_args__ = (
        # Also the access path. A btree on these four columns leads with connection_id, so the
        # Evidence Engine's "every row for this connection" lookup uses this index directly --
        # which is why connection_id below carries no separate index=True. A second index on a
        # column this one already leads with would cost write throughput on every sync and buy
        # nothing.
        UniqueConstraint(
            "connection_id",
            "skill",
            "signal_kind",
            "source_ref",
            name="uq_external_evidence_item_signal",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("provider_connections.id"), nullable=False)

    # Matched against role skill names by the existing skill_matching helpers. String(120)
    # mirrors evidence_state_snapshots.skill_key so the two cannot disagree about what fits.
    skill: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    signal_kind: Mapped[str] = mapped_column(String(40), nullable=False)

    # Provenance, and stable enough to be part of the identity of this signal:
    # "github:repo:octocat/hello-world". Not a URL -- a URL would tempt a client to render it
    # as a link without checking the repository still exists or is still public.
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)

    # When the underlying work was last actually touched (GitHub's `pushed_at` for the
    # repository), NOT when we happened to look. This is what the Evidence Engine's staleness
    # rule reads, and it has to mean "this code is two years old", not "we synced on Tuesday".
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Small normalised extras the UI genuinely renders -- never the provider's raw payload.
    # Storing raw responses would retain far more about the student than the product needs,
    # would age badly as provider schemas change, and would put unreviewed third-party text
    # into our database.
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
