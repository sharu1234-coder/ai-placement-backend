"""Signal collection -- every fact the product already holds about a student, in one shape.

Career Strategy reasons across a dozen features that each store their facts differently: a
0-1 float here, a 0-100 integer there, a status string somewhere else, a JSON blob of per-skill
scores in a fourth place. Comparing them requires one contract, and that contract is
`StrategySignal`. Nothing in this module computes a score, a readiness, an evidence state or a
job match -- every value is read from the service that already owns it and carried across with
its provenance attached, so a later phase can always answer "where did this come from".

Four rules are load-bearing, and each exists because the alternative silently fabricates:

**Unknown is `None`, never 0.** A student with no interview history has
`value=None, evidence_kind="missing"` for interview performance -- not 0.0. A zero would
average into a readiness number and tell them they interview badly, which we did not measure.
This mirrors job_hunt/readiness.py's existing `unknown_dimensions` discipline rather than
inventing a new one.

**Confidence is only carried where a source genuinely has one.** evidence_engine produces a
real, documented confidence (five discrete values keyed to its five states); nothing else here
does. Every other collector leaves `confidence=None` rather than manufacturing a plausible
number to fill the field.

**Freshness is measured, never assumed.** `freshness_days` is populated only when the source
row actually carries a timestamp. A plain profile Project row and a resume keyword have no
date of their own, so theirs stays `None` -- exactly as evidence_engine already refuses to let
undated evidence trigger staleness.

**`now` is injected.** Every clock read enters through one parameter, so the same database
state plus the same `now` produces byte-identical output. `job_hunt/matching.score_match` is
the existing precedent for this.

Performance: every source is fetched exactly once per `collect()` call and matched in Python,
the same approach evidence_engine adopted when its per-skill queries turned out to be a real
N+1 (see `_fetch_profile_sources` there). `collect()` is the only function that touches the
database; everything downstream in this package is pure.
"""

import dataclasses
import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.models.assessment import Assessment
from app.models.assessment_result import AssessmentResult
from app.models.certification import Certification
from app.models.education import Education
from app.models.experience import Experience
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_listing import JobListing
from app.models.job_listing_match import JobListingMatch
from app.models.job_saved_search import JobSavedSearch
from app.models.job_search_profile import JobSearchProfile
from app.models.mission import Mission
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.profile import StudentProfile
from app.models.recruiter_attack_session import RecruiterAttackSession
from app.models.resume import Resume
from app.models.resume_ats_check import ResumeAtsCheck
from app.models.roadmap import Roadmap
from app.models.roadmap_task import RoadmapTask
from app.models.saved_job import SAVED_JOB_STATUSES, SavedJob
from app.models.weakness_record import WeaknessRecord
from app.services import evidence_engine
from app.services.career_strategy import precedence
from app.services.job_hunt import matching
from app.services.role_context import RoleContext, resolve_role_context
from app.services.skill_matching import NoResumeAnalysisError, latest_resume_skills, skill_present
from app.services.weakness_service import INTERVIEW_DIMENSION_THRESHOLD, INTERVIEW_DIMENSIONS, compute_weaknesses

# The signal categories from the product spec's own list (A-O). Kept as a closed tuple so a
# typo'd category is a test failure rather than a silently-unfilterable signal.
SIGNAL_CATEGORIES: Tuple[str, ...] = (
    "identity",
    "career_target",
    "skill_claim",
    "skill_evidence",
    "learning_progress",
    "technical_performance",
    "practical_evidence",
    "resume_quality",
    "ats_alignment",
    "interview_performance",
    "recruiter_performance",
    "job_alignment",
    "application_readiness",
    "activity",
    "placement_progress",
)

# The four ATS sub-scores worth reasoning about strategically. The check also stores parsing,
# structure, contact and readability; those describe the document's mechanics rather than how
# well it represents the candidate, and no gap category acts on them, so they are not carried
# as signals. `overall` is carried separately below.
_ATS_COMPONENTS = ("skills", "content", "experience", "projects")

# Interview dimensions that describe *how the answer was delivered* rather than whether it was
# right. Named here because the strong-technical/weak-communication contradiction needs them
# specifically, and because guessing at that split at the call site would put the definition in
# two places. Subset of weakness_service.INTERVIEW_DIMENSIONS, which stays authoritative for
# which dimensions exist at all.
COMMUNICATION_DIMENSIONS = ("clarity", "structure")

_RECRUITER_DIMENSIONS = ("technical_defense", "project_ownership", "specificity", "communication")

# Profile fields whose absence is a real, actionable prompt rather than a strategic gap. Each
# maps to a field on StudentProfile that the student themselves fills in.
_OPTIONAL_PROFILE_FIELDS = ("degree", "branch", "graduation_year", "experience_level")

# Forward movement through the hiring funnel.
PROGRESSED_STATUSES = ("assessment", "interview", "offer")

# Below this many submitted applications a progression *rate* is noise, not a pattern: one
# application that has not moved yet says nothing about the funnel. Under the floor the rate is
# reported as unknown rather than as 0.0.
MIN_APPLICATIONS_FOR_RATE = 3

# The match components worth aggregating strategically. job_hunt/matching.WEIGHTS also carries
# employment_type_fit and freshness; neither describes anything the student can act on, so they
# are not carried as strategy signals.
_MATCH_COMPONENTS = ("role_alignment", "skill_overlap", "experience_fit", "location_fit")

# Forward movement through the hiring funnel. Deliberately excludes "closed", whose meaning is
# genuinely ambiguous in this schema (the application ended, or the advert closed?) and which is
# documented nowhere -- counting it either way would be a guess presented as a measurement.
PROGRESSED_STATUSES = ("assessment", "interview", "offer")

# "Did they actually apply" is answered by SavedJob.applied_at, not by the status string. The
# model sets it the first time status reaches "applied" and never rewrites it, so it survives a
# later move to rejected/closed -- which a status check would not.


class StrategySignal(BaseModel):
    """One fact, from one source, about one subject.

    Frozen: a signal is a record of something already true elsewhere in the product. Nothing
    downstream may edit one -- a gap classifier that could rewrite a score would be a second
    source of truth, which is the exact thing this layer exists to prevent.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    category: str
    source: str
    subject: Optional[str] = None
    # Normalised to 0-1 wherever the underlying source is a score, so signals are comparable.
    # None means UNKNOWN and must never be read as zero.
    value: Optional[float] = None
    raw_label: str = ""
    observed_at: Optional[datetime.datetime] = None
    evidence_kind: str = "missing"
    # True when this is a direct measurement/observation; False for a derived rollup.
    is_direct: bool = True
    # Populated ONLY where the owning source publishes a real confidence. Never invented.
    confidence: Optional[float] = None
    freshness_days: Optional[int] = None
    # The module that owns this fact, for §77 diagnosability and for "why this?" text later.
    provenance: str = ""


@dataclasses.dataclass(frozen=True)
class SignalBundle:
    """Everything one strategy computation needs, fetched once.

    Carries the raw rows the later pure modules need (assessment results for series analysis,
    the evidence views for gap classification) alongside the flat signal list, so no downstream
    module has to reach back into the database. That keeps gaps.py, contradictions.py and
    diminishing_returns.py pure functions, which is what makes them cheap to test exhaustively.
    """

    profile_id: int
    now: datetime.datetime
    role: Optional[RoleContext]
    # Role skills first, then any resume skill the role does not already name. Ordering is
    # deterministic so downstream iteration is too.
    skills: Tuple[str, ...]
    signals: Tuple[StrategySignal, ...]
    evidence_by_skill: Dict[str, evidence_engine.SkillEvidenceView]
    # None (not []) when the student has no completed resume analysis at all -- the same
    # "we never looked" vs "we looked and found nothing" distinction skill_matching draws.
    resume_skills: Optional[Tuple[str, ...]]
    assessment_results: Tuple[AssessmentResult, ...]
    # (listing, match_or_None) for jobs the student actually saved/tracked.
    tracked_jobs: Tuple[Tuple[JobListing, Optional[JobListingMatch]], ...]
    # Interview Memory rows as themselves. The matching signals carry a human-readable
    # `raw_label` for display; a consumer that needs `status` or `occurrences` as *data* reads
    # them from here rather than parsing that label back apart, so rewording a label can never
    # silently change a severity.
    weakness_records: Tuple[WeaknessRecord, ...]
    # The tracker as the student maintains it, dismissed rows included. Dismissal is a status in
    # the same state machine (see SAVED_JOB_STATUSES), not a separate table, so filtering it out
    # here would lose the fact that the student actively rejected those jobs.
    saved_jobs: Tuple[SavedJob, ...]
    saved_searches: Tuple[JobSavedSearch, ...]
    job_preferences: Optional[JobSearchProfile]
    experience: Tuple[Experience, ...]
    education: Tuple[Education, ...]
    certifications: Tuple[Certification, ...]
    has_resume: bool
    has_interview_history: bool

    def by_key(self, key: str) -> Optional[StrategySignal]:
        for signal in self.signals:
            if signal.key == key:
                return signal
        return None

    def by_category(self, category: str) -> List[StrategySignal]:
        return [signal for signal in self.signals if signal.category == category]

    def for_subject(self, subject: str) -> List[StrategySignal]:
        return [signal for signal in self.signals if signal.subject == subject]


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """Treat a naive datetime as UTC. Mirrors evidence_engine._as_utc for the same reason:
    SQLite has no timezone-aware type, so a value written aware comes back naive, and a
    naive/aware subtraction raises. Every created_at default in this codebase writes UTC, so
    UTC is the only thing a naive value here can mean."""
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)


def _freshness_days(observed_at: Optional[datetime.datetime], now: datetime.datetime) -> Optional[int]:
    if observed_at is None:
        return None
    return (_as_utc(now) - _as_utc(observed_at)).days


def _signal(
    *,
    key: str,
    category: str,
    source: str,
    now: datetime.datetime,
    subject: Optional[str] = None,
    value: Optional[float] = None,
    raw_label: str = "",
    observed_at: Optional[datetime.datetime] = None,
    evidence_kind: str = "missing",
    is_direct: bool = True,
    confidence: Optional[float] = None,
    provenance: str = "",
) -> StrategySignal:
    """Single construction point, so category and evidence-kind validation cannot be skipped
    by a collector that forgets, and so freshness is always derived the same way."""
    if category not in SIGNAL_CATEGORIES:
        raise ValueError(f"Unknown signal category {category!r}")
    precedence.rank(evidence_kind)  # raises UnknownEvidenceKindError on a typo
    return StrategySignal(
        key=key,
        category=category,
        source=source,
        subject=subject,
        value=value,
        raw_label=raw_label,
        observed_at=observed_at,
        evidence_kind=evidence_kind,
        is_direct=is_direct,
        confidence=confidence,
        freshness_days=_freshness_days(observed_at, now),
        provenance=provenance or source,
    )


# -- Evidence ---------------------------------------------------------------------------------

# evidence_engine's five states, mapped onto this layer's evidence-kind vocabulary. `claimed`
# means the item list was empty -- literally nothing found -- which is `missing`, not a claim.
# (The state's name is historical; see evidence_engine._compute_state_and_confidence.) `stale`
# is real evidence that has aged out, so it stays `observed` and lets freshness_days carry the
# age rather than pretending the evidence never existed.
_STATE_TO_KIND = {
    "verified": "verified",
    "strong": "observed",
    "supported": "observed",
    "stale": "observed",
    "claimed": "missing",
}

# Which evidence-kind an individual EvidenceItem represents. Scored items are measurements.
# Self-asserted items are claims, split by where the assertion came from.
def _item_kind(item: evidence_engine.EvidenceItem) -> str:
    if item.self_asserted:
        return "resume_claim" if item.source == "resume" else "user_claim"
    return "observed"


_ITEM_CATEGORY = {
    "assessment": "technical_performance",
    "practice": "technical_performance",
    "project": "practical_evidence",
    "interview": "interview_performance",
    "recruiter_attack": "recruiter_performance",
    "resume": "skill_claim",
}


def _from_evidence(
    views: Sequence[evidence_engine.SkillEvidenceView], now: datetime.datetime
) -> List[StrategySignal]:
    """One rollup signal per skill, plus one signal per underlying evidence item.

    Both levels are needed and neither substitutes for the other: the rollup is what gap
    classification acts on, and the per-item signals are what contradiction detection compares
    (you cannot notice "the resume says AWS but nothing backs it" from an aggregate that has
    already blended the two together).
    """
    signals: List[StrategySignal] = []
    for view in views:
        kind = _STATE_TO_KIND[view.state]
        signals.append(
            _signal(
                key=f"evidence:{view.skill}",
                category="skill_evidence",
                source="evidence_engine",
                now=now,
                subject=view.skill,
                # None for `claimed`: a 0.0 confidence is exactly the "unknown became zero"
                # substitution this layer forbids, even though the engine returns 0.0 there.
                value=None if view.state == "claimed" else view.confidence,
                raw_label=f"{view.state} ({view.confidence_band})",
                observed_at=view.last_verified_at,
                evidence_kind=kind,
                is_direct=False,
                confidence=None if view.state == "claimed" else view.confidence,
                provenance="evidence_engine.get_all_skill_evidence",
            )
        )
        for item in view.items:
            signals.append(
                _signal(
                    key=f"{item.source}:{view.skill}",
                    category=_ITEM_CATEGORY[item.source],
                    source=item.source,
                    now=now,
                    subject=view.skill,
                    value=None if item.score is None else item.score / 100.0,
                    raw_label=item.label,
                    observed_at=item.detected_at,
                    evidence_kind=_item_kind(item),
                    provenance="evidence_engine.EvidenceItem",
                )
            )
    return signals


# -- Career target ----------------------------------------------------------------------------


def _from_role(role: Optional[RoleContext], now: datetime.datetime) -> List[StrategySignal]:
    if role is None:
        return [
            _signal(
                key="career:target_role",
                category="career_target",
                source="role_context",
                now=now,
                raw_label="No target role selected",
                evidence_kind="missing",
                provenance="role_context.resolve_role_context",
            )
        ]
    return [
        _signal(
            key="career:target_role",
            category="career_target",
            source="role_context",
            now=now,
            subject=role.role_family_key,
            raw_label=role.display_role_name,
            # A recorded product fact: the student really did select this. `observed` is the
            # correct kind for "we witnessed this happen", even though the subject is a
            # preference rather than a capability.
            evidence_kind="observed",
            # An AI-assisted role's skill weights are the catalog's best estimate, which
            # role_context already flags; carry that through rather than presenting an
            # estimated weighting as a direct one.
            is_direct=not role.is_estimate,
            provenance=f"role_context({role.role_source})",
        )
    ]


# -- Weaknesses (live) and Interview Memory (persisted) ---------------------------------------


def _from_weaknesses(db: Session, profile_id: int, now: datetime.datetime) -> List[StrategySignal]:
    """weakness_service's own deterministic merge of JD gaps, assessment priority weaknesses
    and interview dimensions. Role-scoped (jd_id=None) -- the JD-scoped variant belongs to a
    job-specific strategy, which is a later phase.

    `value` stays None deliberately: severity is a two-valued label ("high"/"medium"), not a
    measurement, and turning it into 1.0/0.5 would manufacture exactly the kind of fake
    precision weakness_service itself refused to introduce when it stopped substituting 0.5
    for an unscored weakness.
    """
    signals: List[StrategySignal] = []
    for weakness in compute_weaknesses(db, profile_id, jd_id=None):
        signals.append(
            _signal(
                key=f"weakness:{weakness.skill_tag}",
                category="technical_performance",
                source="weakness_service",
                now=now,
                subject=weakness.skill_tag,
                raw_label=f"{weakness.severity} severity, flagged by {', '.join(sorted(weakness.sources))}",
                # A derived aggregate over real assessment/interview/JD rows.
                evidence_kind="inferred",
                is_direct=False,
                provenance="weakness_service.compute_weaknesses",
            )
        )
    return signals


def _from_weakness_records(records: Sequence[WeaknessRecord], now: datetime.datetime) -> List[StrategySignal]:
    """Interview Memory. The persisted cross-interview status machine
    (NEW/REPEATED/IMPROVING/NEARLY_CLOSED/CLOSED) plus an occurrence count -- the thing the
    live weakness view structurally cannot know, since it has no memory of previous
    interviews."""
    signals: List[StrategySignal] = []
    for record in records:
        signals.append(
            _signal(
                key=f"weakness_memory:{record.skill_key}",
                category="interview_performance",
                source="weakness_record_service",
                now=now,
                subject=record.skill_key,
                value=record.last_score,
                raw_label=f"{record.status}, seen {record.occurrences}x, last score {record.last_score:.2f}",
                observed_at=record.last_seen_at,
                evidence_kind="observed",
                provenance="weakness_record_service.list_for_profile",
            )
        )
    return signals


# -- Resume / ATS -----------------------------------------------------------------------------


def _from_resume(
    resume_skills: Optional[Sequence[str]], has_resume: bool, now: datetime.datetime
) -> List[StrategySignal]:
    if not has_resume:
        return [
            _signal(
                key="resume:on_file",
                category="resume_quality",
                source="resume",
                now=now,
                raw_label="No resume uploaded",
                evidence_kind="missing",
                provenance="Resume",
            )
        ]
    # A resume exists but its analysis has not completed (or produced no skills): a real,
    # different state from "no resume", and not something to fill in with an empty list.
    if resume_skills is None:
        return [
            _signal(
                key="resume:on_file",
                category="resume_quality",
                source="resume",
                now=now,
                raw_label="Resume uploaded, analysis not available yet",
                evidence_kind="missing",
                provenance="skill_matching.latest_resume_skills",
            )
        ]
    return [
        _signal(
            key="resume:on_file",
            category="resume_quality",
            source="resume",
            now=now,
            value=None,
            raw_label=f"Resume analysed, {len(resume_skills)} skills detected",
            evidence_kind="observed",
            provenance="skill_matching.latest_resume_skills",
        )
    ]


def _from_ats(check: Optional[ResumeAtsCheck], now: datetime.datetime) -> List[StrategySignal]:
    """The most recent saved ATS check. Scores are 0-100 integers on the row; normalised to
    0-1 here so they sit on the same scale as every other signal. `observed` rather than
    `inferred` because ats_scoring measures the actual document deterministically -- it is a
    real measurement, just of a resume rather than of a capability."""
    if check is None:
        return [
            _signal(
                key="ats:overall",
                category="ats_alignment",
                source="ats_check_service",
                now=now,
                raw_label="No ATS check run yet",
                evidence_kind="missing",
                provenance="ResumeAtsCheck",
            )
        ]

    signals = [
        _signal(
            key="ats:overall",
            category="ats_alignment",
            source="ats_check_service",
            now=now,
            value=check.overall_score / 100.0,
            raw_label=f"ATS {check.overall_score}/100 ({check.score_category})",
            observed_at=check.created_at,
            evidence_kind="observed",
            provenance="ResumeAtsCheck",
        )
    ]
    for component in _ATS_COMPONENTS:
        score = getattr(check, f"{component}_score")
        signals.append(
            _signal(
                key=f"ats:{component}",
                category="ats_alignment",
                source="ats_check_service",
                now=now,
                subject=component,
                value=score / 100.0,
                raw_label=f"ATS {component} {score}/100",
                observed_at=check.created_at,
                evidence_kind="observed",
                provenance="ResumeAtsCheck",
            )
        )
    return signals


# -- Learning progress ------------------------------------------------------------------------


def _from_learning(
    topics: Sequence[PlacementPathTopic], tasks: Sequence[RoadmapTask], now: datetime.datetime
) -> List[StrategySignal]:
    """Per-skill learning progress, merged across the two roadmap-shaped systems that record it
    (Placement Path topics and Roadmap tasks). Both key on `skill_tag`, so a skill covered by
    both contributes to one fraction rather than two competing ones.

    A skill with no topic and no task produces no signal at all -- absence of a plan is not
    zero progress, and emitting 0.0 here would let a later phase conclude "you have learned
    none of this" about a skill nobody ever put on a roadmap.
    """
    done: Dict[str, int] = {}
    total: Dict[str, int] = {}

    for topic in topics:
        total[topic.skill_tag] = total.get(topic.skill_tag, 0) + 1
        if topic.status == "completed":
            done[topic.skill_tag] = done.get(topic.skill_tag, 0) + 1
    for task in tasks:
        total[task.skill_tag] = total.get(task.skill_tag, 0) + 1
        if task.status == "done":
            done[task.skill_tag] = done.get(task.skill_tag, 0) + 1

    signals: List[StrategySignal] = []
    for skill_tag in sorted(total):
        completed = done.get(skill_tag, 0)
        count = total[skill_tag]
        signals.append(
            _signal(
                key=f"learning:{skill_tag}",
                category="learning_progress",
                source="roadmap",
                now=now,
                subject=skill_tag,
                value=completed / count,
                raw_label=f"{completed}/{count} learning items complete",
                evidence_kind="observed",
                provenance="PlacementPathTopic + RoadmapTask",
            )
        )
    return signals


# -- Interviews and Recruiter Attack ----------------------------------------------------------


def _from_interview(
    evaluation: Optional[InterviewEvaluation], now: datetime.datetime
) -> List[StrategySignal]:
    """The most recent completed interview evaluation, dimension by dimension.

    Only the dimensions weakness_service already recognises are carried, so the two modules
    cannot end up disagreeing about which dimensions exist. Voice-only dimensions
    (speaking_pace, filler_word_rate) and grammar are nullable on the row and are skipped when
    absent rather than defaulted.
    """
    if evaluation is None:
        return [
            _signal(
                key="interview:history",
                category="interview_performance",
                source="interview_service",
                now=now,
                raw_label="No completed interview yet",
                evidence_kind="missing",
                provenance="InterviewEvaluation",
            )
        ]

    signals: List[StrategySignal] = []
    for dimension in INTERVIEW_DIMENSIONS:
        score = getattr(evaluation, dimension, None)
        if score is None:
            continue
        signals.append(
            _signal(
                key=f"interview:{dimension}",
                category="interview_performance",
                source="interview_service",
                now=now,
                subject=f"interview:{dimension}",
                value=score,
                raw_label=f"{dimension.replace('_', ' ')} {score:.2f}",
                observed_at=evaluation.created_at,
                evidence_kind="observed",
                provenance="InterviewEvaluation",
            )
        )
    signals.append(
        _signal(
            key="interview:overall",
            category="interview_performance",
            source="interview_service",
            now=now,
            value=evaluation.overall_score,
            raw_label=f"Interview overall {evaluation.overall_score:.2f}",
            observed_at=evaluation.created_at,
            evidence_kind="observed",
            provenance="InterviewEvaluation",
        )
    )
    return signals


def _from_recruiter_attack(
    session: Optional[RecruiterAttackSession], now: datetime.datetime
) -> List[StrategySignal]:
    if session is None:
        return []
    signals: List[StrategySignal] = []
    for dimension in _RECRUITER_DIMENSIONS:
        score = getattr(session, dimension, None)
        if score is None:
            continue
        signals.append(
            _signal(
                key=f"recruiter_attack:{dimension}",
                category="recruiter_performance",
                source="recruiter_attack_service",
                now=now,
                subject=f"recruiter_attack:{dimension}",
                value=score,
                raw_label=f"{dimension.replace('_', ' ')} {score:.2f}",
                observed_at=session.completed_at,
                evidence_kind="observed",
                provenance="RecruiterAttackSession",
            )
        )
    return signals


# -- Jobs -------------------------------------------------------------------------------------


def _from_jobs(
    tracked: Sequence[Tuple[JobListing, Optional[JobListingMatch]]], now: datetime.datetime
) -> List[StrategySignal]:
    """Job alignment, derived exclusively from jobs the student actually saved or tracked and
    from the authoritative match rows already computed for them.

    Nothing here re-scores anything. `missing_skills` is read straight off JobListingMatch,
    which job_hunt/matching produced; this module never calls score_match and never invents a
    requirement a listing did not state. Market demand as a general concept does not appear at
    all -- "required by 3 of your 5 tracked jobs" is a fact about this student's own shortlist,
    which is the only job-demand claim the data can support.

    Match rows from a superseded SCORE_VERSION are ignored rather than migrated: a stale score
    is not authoritative, and silently treating it as current would be this layer asserting
    something the match engine no longer stands behind.
    """
    if not tracked:
        return [
            _signal(
                key="job:tracked_count",
                category="job_alignment",
                source="job_hunt",
                now=now,
                value=None,
                raw_label="No tracked jobs",
                evidence_kind="missing",
                provenance="SavedJob",
            )
        ]

    signals = [
        _signal(
            key="job:tracked_count",
            category="job_alignment",
            source="job_hunt",
            now=now,
            value=None,
            raw_label=f"{len(tracked)} tracked job{'s' if len(tracked) != 1 else ''}",
            evidence_kind="observed",
            provenance="SavedJob",
        )
    ]

    demand: Dict[str, int] = {}
    scored = 0
    for listing, match in tracked:
        if match is None:
            continue
        scored += 1
        signals.append(
            _signal(
                key=f"job_match:{listing.id}",
                category="job_alignment",
                source="job_hunt.matching",
                now=now,
                subject=str(listing.id),
                value=match.total_score,
                raw_label=f"{listing.title} — match {match.total_score:.2f}",
                observed_at=match.calculated_at,
                evidence_kind="observed",
                provenance=f"JobListingMatch({match.score_version})",
            )
        )
        for skill in match.missing_skills or []:
            demand[skill] = demand.get(skill, 0) + 1

    for skill in sorted(demand):
        count = demand[skill]
        signals.append(
            _signal(
                key=f"job_demand:{skill}",
                category="job_alignment",
                source="job_hunt.matching",
                now=now,
                subject=skill,
                value=count / scored if scored else None,
                raw_label=f"Missing for {count} of your {scored} scored tracked jobs",
                evidence_kind="observed",
                provenance="JobListingMatch.missing_skills",
            )
        )
    return signals


# -- Tracker, searches, preferences, background -----------------------------------------------


def _from_tracker(saved_jobs: Sequence[SavedJob], now: datetime.datetime) -> List[StrategySignal]:
    """The student's real hiring funnel, read from the tracker they maintain themselves.

    This is the one place the product can see whether effort is converting into outcomes.
    Applications are counted from `applied_at` rather than from the status string, because the
    model sets that timestamp once and never rewrites it -- a student whose application was
    later rejected still applied, which a status check would miss. "closed" is deliberately not
    interpreted at all: its meaning is genuinely ambiguous in this schema (the application
    ended, or the advert closed?) and documented nowhere, so counting it either way would be a
    guess presented as a measurement.

    Nothing here claims causation. A high application count with no progression is reported as
    a fact about the funnel; deciding what it means is the bottleneck engine's job, and even
    there it is phrased as "may indicate".
    """
    if not saved_jobs:
        return [
            _signal(
                key="tracker:tracked",
                category="placement_progress",
                source="job_hunt.tracker",
                now=now,
                raw_label="No jobs tracked yet",
                evidence_kind="missing",
                provenance="SavedJob",
            )
        ]

    by_status = {status: 0 for status in SAVED_JOB_STATUSES}
    for job in saved_jobs:
        if job.status in by_status:
            by_status[job.status] += 1

    applied = sum(1 for job in saved_jobs if job.applied_at is not None)
    progressed = sum(1 for job in saved_jobs if job.status in PROGRESSED_STATUSES)
    dismissed = by_status.get("dismissed", 0)
    active = len(saved_jobs) - dismissed

    signals = [
        _signal(
            key="tracker:tracked",
            category="placement_progress",
            source="job_hunt.tracker",
            now=now,
            value=None,
            raw_label=f"{active} job{'s' if active != 1 else ''} tracked, {dismissed} dismissed",
            evidence_kind="observed",
            provenance="SavedJob",
        ),
        _signal(
            key="tracker:applied",
            category="placement_progress",
            source="job_hunt.tracker",
            now=now,
            value=None,
            raw_label=f"{applied} application{'s' if applied != 1 else ''} submitted",
            evidence_kind="observed" if applied else "missing",
            provenance="SavedJob.applied_at",
        ),
    ]

    # A progression *rate* only means something once enough applications exist for the ratio not
    # to be noise -- one application that has not moved yet is not a funnel problem. Below the
    # floor the rate is None (unknown), never 0.0, which would read as "nothing ever progresses".
    enough = applied >= MIN_APPLICATIONS_FOR_RATE
    signals.append(
        _signal(
            key="tracker:progression_rate",
            category="placement_progress",
            source="job_hunt.tracker",
            now=now,
            value=(progressed / applied) if enough else None,
            raw_label=(
                f"{progressed} of {applied} applications reached assessment, interview or offer"
                if enough
                else f"{applied} application{'s' if applied != 1 else ''} so far - too few to read a pattern"
            ),
            evidence_kind="observed" if enough else "missing",
            provenance="SavedJob.status",
        )
    )
    return signals


def _from_saved_searches(searches: Sequence[JobSavedSearch], now: datetime.datetime) -> List[StrategySignal]:
    """Saved searches say what the student is actually looking for, which is not always the same
    as the target role they selected. That divergence is what makes a targeting gap visible."""
    if not searches:
        return []
    latest_run = max((s.last_run_at for s in searches if s.last_run_at is not None), default=None)
    return [
        _signal(
            key="search:saved",
            category="job_alignment",
            source="job_hunt.saved_search",
            now=now,
            value=None,
            raw_label=f"{len(searches)} saved search{'es' if len(searches) != 1 else ''}",
            observed_at=latest_run,
            evidence_kind="observed",
            provenance="JobSavedSearch",
        )
    ]


def _from_job_preferences(preferences: Optional[JobSearchProfile], now: datetime.datetime) -> List[StrategySignal]:
    """Stated job-search preferences. These are *preferences*, never capability: a preferred
    employer or a willingness to relocate may surface as a reason and may break a tie, and must
    never become a hidden readiness component.

    `open_to_relocation` is tri-state on the model and stays tri-state here. Unanswered is not
    "no" -- job_hunt/matching._location_score already treats the two differently, and flattening
    them would silently override a preference the student never expressed.
    """
    if preferences is None:
        return [
            _signal(
                key="preference:job_search_profile",
                category="career_target",
                source="job_hunt.preferences",
                now=now,
                raw_label="No job-search preferences set",
                evidence_kind="missing",
                provenance="JobSearchProfile",
            )
        ]

    companies = list(preferences.preferred_companies or [])
    relocation = preferences.open_to_relocation
    if relocation is True:
        relocation_label = "Open to relocation"
    elif relocation is False:
        relocation_label = "Not open to relocation"
    else:
        relocation_label = "Relocation preference not answered"

    return [
        _signal(
            key="preference:preferred_employers",
            category="career_target",
            source="job_hunt.preferences",
            now=now,
            value=None,
            raw_label=(f"{len(companies)} preferred employer(s)" if companies else "No preferred employers named"),
            evidence_kind="observed" if companies else "missing",
            provenance="JobSearchProfile.preferred_companies",
        ),
        _signal(
            key="preference:relocation",
            category="career_target",
            source="job_hunt.preferences",
            now=now,
            value=None,
            raw_label=relocation_label,
            evidence_kind="missing" if relocation is None else "observed",
            provenance="JobSearchProfile.open_to_relocation",
        ),
        _signal(
            key="preference:experience_level",
            category="career_target",
            source="job_hunt.preferences",
            now=now,
            value=None,
            raw_label=preferences.experience_level or "Experience level not set",
            evidence_kind="observed" if preferences.experience_level else "missing",
            provenance="JobSearchProfile.experience_level",
        ),
    ]


def _from_background(
    experience: Sequence[Experience],
    education: Sequence[Education],
    certifications: Sequence[Certification],
    now: datetime.datetime,
) -> List[StrategySignal]:
    """Work history, education and certifications as recorded facts.

    No "years of experience" number is computed here. The product's own vocabulary for seniority
    is the fresher/junior/mid/senior/lead ladder that job_hunt/matching already owns and scores
    against; deriving a competing years figure -- and having to guess how to treat overlapping
    roles, internships and gaps -- would be a second experience model answering a question the
    match engine already answers per job through `component_scores["experience_fit"]`.
    """
    return [
        _signal(
            key="background:experience_entries",
            category="identity",
            source="profile",
            now=now,
            value=None,
            raw_label=(
                f"{len(experience)} role{'s' if len(experience) != 1 else ''} on record"
                if experience
                else "No work history recorded"
            ),
            evidence_kind="observed" if experience else "missing",
            provenance="Experience",
        ),
        _signal(
            key="background:education",
            category="identity",
            source="profile",
            now=now,
            value=None,
            raw_label=(
                f"{len(education)} education entr{'ies' if len(education) != 1 else 'y'}"
                if education
                else "No education recorded"
            ),
            evidence_kind="observed" if education else "missing",
            provenance="Education",
        ),
        _signal(
            key="background:certifications",
            category="practical_evidence",
            source="profile",
            now=now,
            value=None,
            raw_label=(
                f"{len(certifications)} certification{'s' if len(certifications) != 1 else ''}"
                if certifications
                else "No certifications recorded"
            ),
            evidence_kind="observed" if certifications else "missing",
            provenance="Certification",
        ),
    ]


def _from_match_components(
    tracked: Sequence[Tuple[JobListing, Optional[JobListingMatch]]], now: datetime.datetime
) -> List[StrategySignal]:
    """Per-component match scores, averaged across the student's own scored tracked jobs.

    Read straight off JobListingMatch.component_scores, which job_hunt/matching produced. This
    is how experience fit and role alignment enter Career Strategy without a second experience
    model or a second targeting model existing anywhere: the authoritative engine already
    scored both, per job, and this only aggregates what it said.

    A component absent from a match row is skipped rather than defaulted -- matching omits a
    component it could not judge (it lands in `unknown_factors` instead), and substituting 0.0
    would convert "the advert did not say" into "the student does not fit".
    """
    scored = [match for _listing, match in tracked if match is not None]
    if not scored:
        return []

    signals: List[StrategySignal] = []
    for component in _MATCH_COMPONENTS:
        values = [
            match.component_scores[component]
            for match in scored
            if isinstance(match.component_scores, dict) and match.component_scores.get(component) is not None
        ]
        if not values:
            signals.append(
                _signal(
                    key=f"match_component:{component}",
                    category="job_alignment",
                    source="job_hunt.matching",
                    now=now,
                    subject=component,
                    raw_label=f"{component.replace('_', ' ')} could not be judged on your tracked jobs",
                    evidence_kind="missing",
                    provenance="JobListingMatch.component_scores",
                )
            )
            continue
        mean = sum(values) / len(values)
        signals.append(
            _signal(
                key=f"match_component:{component}",
                category="job_alignment",
                source="job_hunt.matching",
                now=now,
                subject=component,
                value=mean,
                raw_label=f"{component.replace('_', ' ')} averages {mean:.0%} across {len(values)} tracked jobs",
                evidence_kind="observed",
                is_direct=False,
                provenance="JobListingMatch.component_scores",
            )
        )
    return signals


# -- Profile completeness, application-readiness inputs, activity ------------------------------


def _from_profile(profile: StudentProfile, now: datetime.datetime) -> List[StrategySignal]:
    signals: List[StrategySignal] = []
    for field in _OPTIONAL_PROFILE_FIELDS:
        value = getattr(profile, field, None)
        present = value is not None and str(value).strip() != ""
        signals.append(
            _signal(
                key=f"profile:{field}",
                category="identity",
                source="profile",
                now=now,
                subject=field,
                raw_label=str(value) if present else f"{field} not set",
                evidence_kind="observed" if present else "missing",
                provenance="StudentProfile",
            )
        )
    return signals


def _from_readiness_inputs(
    has_resume: bool, has_interview_history: bool, now: datetime.datetime
) -> List[StrategySignal]:
    """The two application-readiness inputs that are the student's to provide, and that
    job_hunt/readiness.py would report as `unknown_dimensions` for *every* job until they
    exist. Deliberately not a call into readiness.compute: that function is job-scoped and
    needs a listing, and reproducing its per-job maths here would be the second readiness
    algorithm this layer is forbidden to create. These signals say only what is missing, which
    is listing-independent and true."""
    return [
        _signal(
            key="readiness_input:resume",
            category="application_readiness",
            source="job_hunt.readiness",
            now=now,
            subject="resume",
            raw_label="Resume on file" if has_resume else "No resume — readiness cannot be judged on it",
            evidence_kind="observed" if has_resume else "missing",
            provenance="job_hunt/readiness.py inputs",
        ),
        _signal(
            key="readiness_input:interview",
            category="application_readiness",
            source="job_hunt.readiness",
            now=now,
            subject="interview",
            raw_label=(
                "Interview history available"
                if has_interview_history
                else "No interview history — readiness cannot be judged on it"
            ),
            evidence_kind="observed" if has_interview_history else "missing",
            provenance="job_hunt/readiness.py inputs",
        ),
    ]


def _from_activity(missions: Sequence[Mission], now: datetime.datetime) -> List[StrategySignal]:
    completed = sum(1 for mission in missions if mission.status == "completed")
    return [
        _signal(
            key="activity:missions_completed",
            category="activity",
            source="mission_service",
            now=now,
            value=None,
            raw_label=f"{completed} of {len(missions)} missions completed",
            evidence_kind="observed" if missions else "missing",
            provenance="Mission",
        )
    ]


# -- Fetch ------------------------------------------------------------------------------------


def _skill_universe(role: Optional[RoleContext], resume_skills: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """Role skills first (they are what the student is being measured against), then resume
    skills the role does not already name.

    The second half matters for one specific case: a student with genuinely strong evidence in
    skills their *target role* does not weight. Without it, "your strengths are real but they
    are not the ones this role needs" is undetectable, because we would only ever have looked
    at the role's own skill list. Matching uses skill_matching.skill_present so "AWS" and
    "aws" do not become two skills.
    """
    ordered: List[str] = list(role.weights.keys()) if role is not None else []
    for skill in resume_skills or []:
        if not any(skill_present(existing, [skill]) for existing in ordered):
            ordered.append(skill)
    return tuple(ordered)


def collect(
    db: Session,
    profile: StudentProfile,
    *,
    now: Optional[datetime.datetime] = None,
    role: Optional[RoleContext] = None,
) -> SignalBundle:
    """Fetch every source once and return the full signal set.

    The only database-touching function in this package. `now` defaults to the wall clock but
    every caller in tests injects it, which is what makes the rest of the pipeline provably
    deterministic.

    `role` overrides which target the student is measured against, and is the entire mechanism
    behind multi-target strategy. Everything downstream -- gaps, contradictions, prioritisation,
    the bottleneck -- already reads the role from this bundle, so swapping it here runs the
    identical engine against a different target instead of standing up a second one. Left as
    None it resolves the student's real target exactly as before, which is why the single-target
    path is unaffected by this parameter existing.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    profile_id = profile.id

    role = role if role is not None else resolve_role_context(db, profile)

    try:
        resume_skill_list: Optional[List[str]] = latest_resume_skills(db, profile_id)
    except NoResumeAnalysisError:
        resume_skill_list = None

    has_resume = db.query(Resume.id).filter(Resume.profile_id == profile_id).first() is not None

    skills = _skill_universe(role, resume_skill_list)
    views = evidence_engine.get_all_skill_evidence(db, profile_id, list(skills)) if skills else []

    assessment_results = (
        db.query(AssessmentResult)
        .join(Assessment, Assessment.id == AssessmentResult.assessment_id)
        .filter(Assessment.profile_id == profile_id)
        .order_by(AssessmentResult.created_at.asc(), AssessmentResult.id.asc())
        .all()
    )

    ats_check = (
        db.query(ResumeAtsCheck)
        .filter(ResumeAtsCheck.profile_id == profile_id)
        .order_by(ResumeAtsCheck.id.desc())
        .first()
    )

    topics = (
        db.query(PlacementPathTopic)
        .join(PlacementPath, PlacementPath.id == PlacementPathTopic.path_id)
        .filter(PlacementPath.profile_id == profile_id, PlacementPath.status == "active")
        .all()
    )
    tasks = (
        db.query(RoadmapTask)
        .join(Roadmap, Roadmap.id == RoadmapTask.roadmap_id)
        .filter(Roadmap.profile_id == profile_id, Roadmap.status == "active")
        .all()
    )

    evaluation = (
        db.query(InterviewEvaluation)
        .join(Interview, Interview.id == InterviewEvaluation.interview_id)
        .filter(Interview.profile_id == profile_id)
        .order_by(InterviewEvaluation.created_at.desc(), InterviewEvaluation.id.desc())
        .first()
    )

    attack_session = (
        db.query(RecruiterAttackSession)
        .filter(
            RecruiterAttackSession.profile_id == profile_id,
            RecruiterAttackSession.overall_score.isnot(None),
        )
        .order_by(RecruiterAttackSession.completed_at.desc(), RecruiterAttackSession.id.desc())
        .first()
    )

    tracked_rows = (
        db.query(JobListing, SavedJob)
        .join(SavedJob, SavedJob.listing_id == JobListing.id)
        .filter(SavedJob.profile_id == profile_id)
        .order_by(JobListing.id.asc())
        .all()
    )
    listing_ids = [listing.id for listing, _saved in tracked_rows]
    matches = (
        db.query(JobListingMatch)
        .filter(
            JobListingMatch.profile_id == profile_id,
            JobListingMatch.listing_id.in_(listing_ids),
            JobListingMatch.score_version == matching.SCORE_VERSION,
        )
        .all()
        if listing_ids
        else []
    )
    match_by_listing = {match.listing_id: match for match in matches}
    tracked = tuple((listing, match_by_listing.get(listing.id)) for listing, _saved in tracked_rows)

    weakness_records = db.query(WeaknessRecord).filter(WeaknessRecord.profile_id == profile_id).all()
    missions = db.query(Mission).filter(Mission.profile_id == profile_id).all()

    # The whole tracker in one query. Answering "is this saved?" or "how many applied?" by
    # querying per listing is the N+1 this codebase has already had to fix once elsewhere.
    saved_jobs = db.query(SavedJob).filter(SavedJob.profile_id == profile_id).all()
    saved_searches = db.query(JobSavedSearch).filter(JobSavedSearch.profile_id == profile_id).all()
    job_preferences = (
        db.query(JobSearchProfile)
        .filter(JobSearchProfile.profile_id == profile_id)
        .order_by(JobSearchProfile.id.desc())
        .first()
    )
    experience_rows = db.query(Experience).filter(Experience.profile_id == profile_id).all()
    education_rows = db.query(Education).filter(Education.profile_id == profile_id).all()
    certification_rows = db.query(Certification).filter(Certification.profile_id == profile_id).all()

    signals: List[StrategySignal] = []
    signals += _from_role(role, now)
    signals += _from_evidence(views, now)
    signals += _from_weaknesses(db, profile_id, now)
    signals += _from_weakness_records(weakness_records, now)
    signals += _from_resume(resume_skill_list, has_resume, now)
    signals += _from_ats(ats_check, now)
    signals += _from_learning(topics, tasks, now)
    signals += _from_interview(evaluation, now)
    signals += _from_recruiter_attack(attack_session, now)
    signals += _from_jobs(tracked, now)
    signals += _from_profile(profile, now)
    signals += _from_readiness_inputs(has_resume, evaluation is not None, now)
    signals += _from_activity(missions, now)
    signals += _from_tracker(saved_jobs, now)
    signals += _from_saved_searches(saved_searches, now)
    signals += _from_job_preferences(job_preferences, now)
    signals += _from_background(experience_rows, education_rows, certification_rows, now)
    signals += _from_match_components(tracked, now)

    return SignalBundle(
        profile_id=profile_id,
        now=now,
        role=role,
        skills=skills,
        signals=tuple(signals),
        evidence_by_skill={view.skill: view for view in views},
        resume_skills=tuple(resume_skill_list) if resume_skill_list is not None else None,
        assessment_results=tuple(assessment_results),
        tracked_jobs=tracked,
        weakness_records=tuple(weakness_records),
        saved_jobs=tuple(saved_jobs),
        saved_searches=tuple(saved_searches),
        job_preferences=job_preferences,
        experience=tuple(experience_rows),
        education=tuple(education_rows),
        certifications=tuple(certification_rows),
        has_resume=has_resume,
        has_interview_history=evaluation is not None,
    )


def is_interview_subject(subject: Optional[str]) -> bool:
    """True for the namespaced dimension tags ("interview:clarity",
    "recruiter_attack:specificity") that weakness_service and weakness_record_service produce.

    These are *not* skills. They describe how an answer was delivered, not a technology, and
    letting one into role-skill matching would produce a Career Twin row for "clarity" with a
    role weight and a recommendation to go practise it as if it were Kubernetes. Every
    role-skill loop in this package filters on this predicate, and it is tested directly.
    """
    if subject is None:
        return False
    return subject.startswith("interview:") or subject.startswith("recruiter_attack:")


def interview_dimension_is_weak(value: Optional[float]) -> Optional[bool]:
    """None when unmeasured -- deliberately tri-state. `False` would claim we checked and found
    it fine. Threshold is weakness_service's, imported rather than redeclared so the two can
    never drift."""
    if value is None:
        return None
    return value < INTERVIEW_DIMENSION_THRESHOLD
