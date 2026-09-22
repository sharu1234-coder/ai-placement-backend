import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

# The whole state machine, kept here rather than in the service because the model's own
# validation and the API schema both need the same list and a second copy would be free to
# drift -- the same reasoning, and the same placement, as SavedJob's SAVED_JOB_STATUSES.
#
# Four values, deliberately. There is no "applied"/"interviewing"/"offer" pipeline here: that
# is SavedJob's job, and it belongs to a listing the student found, not to a goal they wrote
# down. A CareerGoal answers one question -- "am I preparing for this?" -- so its states are
# only the answers to that.
CAREER_GOAL_STATUSES = ("active", "paused", "completed", "archived")

# Archiving rather than deleting is the default for a goal the student is done with, matching
# how the rest of this app treats a student's own history. Deletion stays available, because a
# goal created by mistake is not history worth keeping.
CAREER_GOAL_STATUS_ACTIVE = "active"
CAREER_GOAL_STATUS_ARCHIVED = "archived"


class CareerGoal(Base):
    """One job a student has decided to prepare for, and the thing their preparation is
    measured against.

    Distinct from everything nearby, and the distinctions are the point:

    - `JobDescription` is an *analysis artifact* -- a piece of text that was parsed once. It
      carries no intent, no state, and is thrown away when the student deletes the analysis.
    - `SavedJob` is a student's relationship with a real `JobListing` they found through Job
      Hunt, and tracks an application pipeline.
    - `CareerStrategyTarget` is a *role* ("also show me my strategy as a Data Engineer"), not
      a specific opening at a specific company.

    A CareerGoal is the one place that says "Data Engineer, at Microsoft, in Dubai, and I am
    preparing for it." That intent is what makes it worth persisting; nothing else here does.

    It deliberately stores no analysis of its own. Alignment, evidence, interview risk,
    preparation progress and the next action are all recomputed on read from the engines that
    already own them (evidence_engine, weakness_service, next_best_action_service), because a
    copy here would be a second source of truth that goes stale the moment the student
    finishes a practice session. The only thing this table caches is `jd_id` -- a pointer to
    the parse, not the parse itself.

    Ownership is by profile_id and is never accepted from a client; see
    career_goal_service._get_owned.
    """

    __tablename__ = "career_goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("student_profiles.id"), nullable=False, index=True
    )

    # What the STUDENT says this job is. Deliberately not read back from the parsed JD:
    # JobDescription.role_title/location are the AI's best guess at what the text describes,
    # and a student who wrote "Data Engineer II, Microsoft, Dubai" should see exactly that on
    # their own goal even if the parser summarised the posting as "Data Engineer". Stated and
    # detected are different provenance, and this codebase keeps those apart everywhere else
    # (see EvidenceItem.self_asserted, Requirement.is_explicit).
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    # Optional because remote/unstated postings are ordinary, and an empty string forced into
    # a required field would be a worse lie than an honest NULL.
    location: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # The posting text, exactly as the student supplied it. UNTRUSTED CONTENT: it is never
    # interpolated into a prompt from here -- analysis goes through
    # jd_service.parse_job_description, whose prompt path already neutralizes delimiters.
    #
    # Held on the goal rather than only on the JobDescription row because the goal must
    # survive an AI failure: before the first successful analysis there is no JobDescription
    # at all, and losing the student's pasted text because a provider was briefly down would
    # make the "try again" this feature promises impossible to honour.
    job_description: Mapped[str] = mapped_column(Text, nullable=False)

    # The parse this goal's analysis currently reads from. NULL means "never successfully
    # analyzed", which is a real, displayable state and NOT an error -- exactly the Phase 2C
    # distinction between ungraded and not-yet-evaluated. Re-analysing repoints this at a new
    # parse; the old one is deleted, so a goal never accumulates stale analyses.
    jd_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("job_descriptions.id"), nullable=True, index=True
    )

    # See CAREER_GOAL_STATUSES. Indexed because the list endpoint's normal case is "the goals
    # I am actually working on", i.e. a filter on this column.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CAREER_GOAL_STATUS_ACTIVE, index=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
