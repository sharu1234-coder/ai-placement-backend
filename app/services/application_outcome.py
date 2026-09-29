"""What a real-world job application currently says, derived from the row that already records
it.

This module stores nothing and decides nothing. SavedJob.status is and remains the single
source of truth for where an application stands (see app/models/saved_job.py's
SAVED_JOB_STATUSES); everything here is a pure reading of that column, kept in one place so
career_goal_service, career_twin_service and next_best_action_service cannot each grow their
own slightly different idea of what "rejected" means.

Deliberately NOT an engine. No AI, no persistence, no scoring, no ranking -- three dictionaries
and a count. An application outcome is a fact about the external world, and the honest thing to
do with a fact is report it, not weight it.

THE ONE THING THIS MODULE MUST NEVER DO is turn an outcome into a statement about the
candidate's skills. A rejection is not evidence that any particular skill is missing: companies
reject for budget, timing, visa, internal candidates and hundreds of reasons no code here can
see. Evidence about skills comes from evidence_engine; weaknesses come from weakness_service.
This module's output sits beside those, never inside them.
"""

import dataclasses
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.saved_job import SavedJob

# The terminal outcomes an application can end in. Three, not eight: these are the values a
# caller branches on, while SavedJob.status keeps the finer distinction for display.
APPLICATION_OUTCOMES = ("offer", "rejected", "closed")

# status -> terminal outcome, or absent when the application is still open.
#
# "dismissed" maps to "closed" rather than to an outcome of its own: the student decided not to
# pursue the job, which ends the application without the employer having said anything. That is
# the same shape as "closed" for every consumer here, and inventing a fourth outcome to record
# a distinction nobody branches on would be precision this product cannot use. SavedJob.status
# still says "dismissed" for display, so nothing is lost.
_OUTCOME_BY_STATUS: Dict[str, str] = {
    "offer": "offer",
    "rejected": "rejected",
    "closed": "closed",
    "dismissed": "closed",
}

# Statuses meaning "the student has actually applied and the process is still running".
# "saved" is excluded because saving a job is an intention, not an application -- the same
# distinction CareerGoal itself draws.
_LIVE_STATUSES = frozenset({"applied", "assessment", "interview"})

# Statuses meaning "this application has been submitted", live or finished. Used for
# "has the student ever actually applied", which "saved" alone must not answer yes to.
_SUBMITTED_STATUSES = frozenset({"applied", "assessment", "interview", "offer", "rejected"})


def outcome_for(status: str) -> Optional[str]:
    """The terminal outcome of an application in this status, or None while it is still open.

    None is the correct answer for "saved", "applied", "assessment" and "interview" -- not an
    empty string and not "pending". The process genuinely has no outcome yet, and saying so is
    different from saying it went badly.
    """
    return _OUTCOME_BY_STATUS.get(status)


def is_terminal(status: str) -> bool:
    """Whether this application has finished, however it finished."""
    return status in _OUTCOME_BY_STATUS


def is_live(status: str) -> bool:
    """Whether the student has applied and is still waiting on the employer."""
    return status in _LIVE_STATUSES


def has_applied(status: str) -> bool:
    """Whether an application was ever actually submitted. False for "saved" and "dismissed"."""
    return status in _SUBMITTED_STATUSES


@dataclasses.dataclass(frozen=True)
class ApplicationOutcomeContext:
    """A count of what has actually happened to this student's applications.

    Counts, not scores. There is no formula here and no number between 0 and 100, because
    there is no defensible one: three rejections and one offer does not reduce to a percentage
    of anything, and pretending it does would be exactly the manufactured precision this
    codebase refuses everywhere else.

    `at_interview_stage` deserves its awkward name. SavedJob stores current state, not history,
    so a job now marked "rejected" may or may not have passed through an interview -- we cannot
    know, and this field does not guess. It counts applications sitting at the interview stage
    right now, which is the only thing the data supports.
    """

    total: int
    live: int
    at_interview_stage: int
    offers: int
    rejections: int
    closed: int

    @property
    def has_any_application(self) -> bool:
        return self.total > 0

    @property
    def has_live_application(self) -> bool:
        return self.live > 0


_EMPTY_CONTEXT = ApplicationOutcomeContext(
    total=0, live=0, at_interview_stage=0, offers=0, rejections=0, closed=0
)


def context_for_profile(db: Session, profile_id: int) -> ApplicationOutcomeContext:
    """Summarise every application this student is tracking, in one query.

    One query on purpose: this runs inside career twin and next-best-action reads, which are
    already the two heaviest composed responses in the product, and a per-application round
    trip there would be the N+1 the evidence engine was specifically designed to avoid.
    """
    statuses: List[str] = [row[0] for row in db.query(SavedJob.status).filter(SavedJob.profile_id == profile_id).all()]
    return context_for_statuses(statuses)


def context_for_statuses(statuses: List[str]) -> ApplicationOutcomeContext:
    """The counting itself, split out so it can be tested without a database."""
    if not statuses:
        return _EMPTY_CONTEXT
    outcomes = [outcome_for(s) for s in statuses]
    return ApplicationOutcomeContext(
        total=len(statuses),
        live=sum(1 for s in statuses if is_live(s)),
        at_interview_stage=sum(1 for s in statuses if s == "interview"),
        offers=sum(1 for o in outcomes if o == "offer"),
        rejections=sum(1 for o in outcomes if o == "rejected"),
        closed=sum(1 for o in outcomes if o == "closed"),
    )
