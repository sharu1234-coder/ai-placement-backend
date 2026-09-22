"""Career Goal -- "prepare me for THIS job".

An orchestration layer, and deliberately nothing more. Every judgement it reports is made by an
engine that already existed before this module:

    what the job asks for      jd_service.parse_job_description -> Requirement rows
    what the candidate can show evidence_engine.get_all_skill_evidence
    what to practise           weakness_service.compute_weaknesses (already jd_id-aware)
    cross-interview memory     weakness_record_service's WeaknessRecord rows
    what to do next            next_best_action_service

There is no scoring here, no model of the candidate, no second recommendation engine, and no
prompt of its own. The one AI call in the whole feature is `analyze`, which delegates to the
existing JD parser -- so the existing delimiter neutralisation, the existing "never invent a
requirement" prompt rules and the existing is_explicit grounding all apply unchanged, and
Phase 2D adds no new prompt-injection surface.

Two rules shape everything below.

*Absence is not a verdict.* A skill with no evidence is reported as "no evidence found", never
as "the candidate does not have this skill", and never as a zero. Where there is nothing to
measure, the number is NULL -- the same rule Phase 2A-0 applied to Career Path readiness and
Phase 2C to an ungraded evaluation.

*The goal outlives its analysis.* The job description lives on the goal itself, so a failed or
never-run analysis costs the student nothing but a retry.
"""

import datetime
import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider
from app.models.career_goal import CAREER_GOAL_STATUS_ACTIVE, CareerGoal
from app.models.interview import Interview
from app.models.job_description import JobDescription
from app.models.profile import StudentProfile
from app.models.requirement import Requirement
from app.models.weakness_record import WeaknessRecord
from app.schemas.career_goal import (
    CareerGoalAnalyzeResponse,
    CareerGoalCreate,
    CareerGoalDetailRead,
    CareerGoalListItem,
    CareerGoalUpdate,
    GoalInterviewRiskRead,
    GoalPreparationItemRead,
    GoalProgressRead,
    GoalRequirementRead,
    GoalSkillEvidenceRead,
)
from app.services import evidence_engine, jd_service, next_best_action_service, weakness_service

logger = logging.getLogger(__name__)


class CareerGoalNotFoundError(Exception):
    """Raised for a goal that does not exist AND for one belonging to somebody else.

    One exception for both, mapped to 404 by the router, because distinguishing them would
    turn this endpoint into an oracle for which goal ids exist -- the same convention the rest
    of this app's nested resources already follow.
    """


# At most this many goals per profile. Not a paywall: a bound so that one account cannot turn a
# free-text field with a 20,000 character ceiling into unbounded storage, and so the list
# endpoint's response size is knowable.
MAX_GOALS_PER_PROFILE = 25

# How a skill's evidence state becomes the three-way answer this phase is built around.
# Derived, never stored, so it cannot drift from the state it came from.
#
#   claimed    zero evidence items of any kind.
#   supported  one real source, OR any number of purely self-asserted ones (a resume keyword,
#              a typed-in project). Real but thin either way -- hence "limited", which is
#              exactly the CLAIMED/LIMITED EVIDENCE case the product spec describes.
#   stale      real evidence, but every dated piece of it has aged past evidence_engine's
#              90-day window. Something was shown once; it is no longer safe to imply it is
#              current, so it needs attention rather than counting as alignment.
#   strong     breadth plus at least one source the student did not merely assert.
#   verified   breadth plus depth.
_EVIDENCE_VERDICTS = {
    "verified": ("evidence_found", "Evidence found"),
    "strong": ("evidence_found", "Evidence found"),
    "supported": ("limited_evidence", "Limited evidence"),
    "stale": ("limited_evidence", "Limited evidence, and not recent"),
    "claimed": ("no_evidence", "No evidence found"),
}

# The states that count as alignment for evidence_coverage_percent, and for the ALIGNED bucket.
# Deliberately excludes "supported": a single self-asserted mention is not something the
# student can defend in an interview, which is what this whole feature is preparing them for.
_ALIGNED_STATES = frozenset({"strong", "verified"})

# Only skill-shaped requirements can be matched against evidence. A responsibility
# ("Own the ingestion pipeline") or an education line is real and is shown, but there is
# nothing in the Evidence Engine it could be looked up against, so it is never silently
# reported as a gap. Same category set jd_grounding._MATCHABLE_CATEGORIES uses.
_MATCHABLE_CATEGORIES = ("skill", "tool")

# Evidence coverage at or above this counts as prepared, for _preparation_state only. A single
# documented constant rather than a tuned curve: the number is shown alongside the state, so a
# student can always see the input that produced it.
_READY_EVIDENCE_COVERAGE = 70

_INTERVIEW_TAG_PREFIX = "interview:"


# ==========================================================================================
# Ownership
# ==========================================================================================


def _get_owned(db: Session, profile_id: int, career_goal_id: int) -> CareerGoal:
    """The only way this module ever loads a goal.

    profile_id always comes from the authenticated user's own profile; it is never read from
    the request body or the path. A goal belonging to someone else is indistinguishable here
    from one that does not exist.
    """
    goal = db.get(CareerGoal, career_goal_id)
    if goal is None or goal.profile_id != profile_id:
        raise CareerGoalNotFoundError("Career goal not found")
    return goal


def _touch(goal: CareerGoal) -> None:
    goal.updated_at = datetime.datetime.now(datetime.timezone.utc)


# ==========================================================================================
# CRUD
# ==========================================================================================


class TooManyCareerGoalsError(Exception):
    pass


def create(db: Session, profile_id: int, payload: CareerGoalCreate) -> CareerGoalListItem:
    """Create a goal. Deliberately runs no AI.

    Analysis is a separate, explicit step. Pasting a job description must always succeed and
    always be saved -- if the provider is down at that moment, the student keeps their goal and
    their text, and presses Analyze again later.
    """
    existing = db.query(CareerGoal).filter(CareerGoal.profile_id == profile_id).count()
    if existing >= MAX_GOALS_PER_PROFILE:
        raise TooManyCareerGoalsError(
            f"You can track up to {MAX_GOALS_PER_PROFILE} career goals at a time. "
            "Archive or delete one to add another."
        )

    goal = CareerGoal(
        profile_id=profile_id,
        title=payload.title.strip(),
        company=payload.company.strip(),
        location=_clean_optional(payload.location),
        job_description=payload.job_description,
        status=CAREER_GOAL_STATUS_ACTIVE,
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return _list_item(db, goal)


def _clean_optional(value: Optional[str]) -> Optional[str]:
    """An empty or whitespace-only optional field becomes NULL rather than "". Storing the
    empty string would make "they did not say" and "they said nothing" indistinguishable."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def list_goals(db: Session, profile_id: int) -> List[CareerGoalListItem]:
    goals = (
        db.query(CareerGoal)
        .filter(CareerGoal.profile_id == profile_id)
        .order_by(CareerGoal.updated_at.desc(), CareerGoal.id.desc())
        .limit(MAX_GOALS_PER_PROFILE)
        .all()
    )
    return [_list_item(db, goal) for goal in goals]


def update(
    db: Session, profile_id: int, career_goal_id: int, payload: CareerGoalUpdate
) -> CareerGoalListItem:
    """Apply a partial update.

    Editing the job description clears the analysis rather than leaving the old one attached:
    an alignment computed from text the student has since replaced is worse than no alignment,
    because it looks current.
    """
    goal = _get_owned(db, profile_id, career_goal_id)

    if payload.title is not None:
        goal.title = payload.title.strip()
    if payload.company is not None:
        goal.company = payload.company.strip()
    if payload.location is not None:
        goal.location = _clean_optional(payload.location)
    if payload.status is not None:
        goal.status = payload.status
    if payload.job_description is not None and payload.job_description != goal.job_description:
        goal.job_description = payload.job_description
        _discard_analysis(db, goal)

    _touch(goal)
    db.commit()
    db.refresh(goal)
    return _list_item(db, goal)


def delete(db: Session, profile_id: int, career_goal_id: int) -> None:
    """Hard delete, with the goal's own parse.

    Archiving is available as a status and is what the UI offers first; this is the escape
    hatch for a goal created by mistake. The parse is removed with it because it was created
    by and for this goal -- leaving it behind would orphan a row nothing can reach.
    """
    goal = _get_owned(db, profile_id, career_goal_id)
    _discard_analysis(db, goal)
    db.delete(goal)
    db.commit()


def _discard_analysis(db: Session, goal: CareerGoal) -> None:
    """Detach and delete whatever parse this goal currently points at."""
    jd_id = goal.jd_id
    goal.jd_id = None
    _delete_parse(db, jd_id)


def _delete_parse(db: Session, jd_id: Optional[int]) -> None:
    """Delete one parse, requirements first.

    Not a cascade: doing it explicitly keeps the ordering visible, and means a foreign-key
    error here is a bug that surfaces rather than a row silently disappearing.
    """
    if jd_id is None:
        return
    db.query(Requirement).filter(Requirement.jd_id == jd_id).delete(synchronize_session=False)
    jd = db.get(JobDescription, jd_id)
    if jd is not None:
        db.delete(jd)


# ==========================================================================================
# Analysis -- the one AI call in the feature
# ==========================================================================================


def analyze(
    db: Session, profile_id: int, career_goal_id: int, ai_provider: AIProvider
) -> CareerGoalAnalyzeResponse:
    """Parse this goal's job description into grounded requirements.

    Delegates wholesale to jd_service.parse_job_description, which means:
      - the job description keeps being treated as untrusted text, by the same code path that
        already neutralises delimiters before it reaches a prompt;
      - `is_explicit` still separates what the posting actually says from what the parser
        inferred, so nothing here can invent a requirement;
      - there is no second JD prompt to keep in step with the first.

    AIGenerationError is deliberately NOT caught. The router turns it into the app's standard
    provider-agnostic 422 (see ai_errors.ai_unavailable), and the goal is untouched -- no
    partial parse, no empty analysis, no fabricated result, and the student can retry. Swallowed
    here it would become an analysis that silently found nothing.
    """
    goal = _get_owned(db, profile_id, career_goal_id)
    # The parse this goal pointed at when this request started. It is the compare half of the
    # compare-and-swap below, so it has to be read before the (slow) AI call, not after.
    previous_jd_id = goal.jd_id

    parsed = jd_service.parse_job_description(
        db, profile_id, goal.job_description, ai_provider, source="career_goal"
    )

    # Attach the new parse only if nothing changed the goal's analysis while this one was
    # running. Two taps on Analyze otherwise both parse, both attach, and the second one's
    # _discard leaves the first one's JobDescription and Requirement rows behind with nothing
    # pointing at them -- not a crash, but a leak that grows with every double tap.
    #
    # Same rowcount-decides-the-winner primitive as mission_service.complete and
    # interview_service._grade_ungraded_row, in its compare-and-swap form because the value
    # being claimed is not a fixed NULL: a first analysis swaps NULL -> new, a re-analysis
    # swaps old -> new, and both must reject a concurrent writer. `== None` renders as IS NULL,
    # so the two cases need different comparisons rather than one parameterised one.
    previous_matches = (
        CareerGoal.jd_id.is_(None) if previous_jd_id is None else CareerGoal.jd_id == previous_jd_id
    )
    result = db.execute(
        sql_update(CareerGoal)
        .where(CareerGoal.id == goal.id, previous_matches)
        .values(jd_id=parsed.id, updated_at=datetime.datetime.now(datetime.timezone.utc))
    )
    db.commit()

    if not result.rowcount:
        # Lost the swap. Delete THIS call's parse, never the winner's -- the student's goal
        # now shows a complete, current analysis either way, and exactly one parse survives.
        logger.info(
            "Career goal %s was analyzed concurrently; discarding this request's parse and "
            "returning the stored one",
            career_goal_id,
        )
        _delete_parse(db, parsed.id)
        db.commit()
        db.refresh(goal)
        return _analyze_response(db, goal)

    # Won. The parse this one replaced is now unreachable, so it goes.
    _delete_parse(db, previous_jd_id)
    db.commit()
    db.refresh(goal)

    response = _analyze_response(db, goal)
    logger.info(
        "Career goal %s analyzed: %d requirements, %d matchable skills",
        career_goal_id, response.requirements_found, response.skills_found,
    )
    return response


def _analyze_response(db: Session, goal: CareerGoal) -> CareerGoalAnalyzeResponse:
    """Built from what is actually stored, not from what this call computed -- which is what
    lets the loser of the swap above describe the winner's analysis truthfully."""
    if goal.jd_id is None:
        return CareerGoalAnalyzeResponse(id=goal.id, is_analyzed=False)
    requirements = db.query(Requirement).filter(Requirement.jd_id == goal.jd_id).all()
    jd = db.get(JobDescription, goal.jd_id)
    return CareerGoalAnalyzeResponse(
        id=goal.id,
        is_analyzed=True,
        detected_role_title=jd.role_title if jd is not None else None,
        requirements_found=len(requirements),
        skills_found=len([r for r in requirements if r.category in _MATCHABLE_CATEGORIES]),
    )


# ==========================================================================================
# The detail view -- deterministic, no AI
# ==========================================================================================


def get_detail(db: Session, profile: StudentProfile, career_goal_id: int) -> CareerGoalDetailRead:
    """Compose the whole job-specific view from existing engines. Runs no AI at all.

    That is what makes it safe to open this screen as often as the student likes: every number
    here is recomputed from rows that already exist, so it is always current and costs nothing.
    """
    goal = _get_owned(db, profile.id, career_goal_id)

    requirements = _requirements_for(db, goal)
    skill_views = _skill_evidence(db, profile.id, requirements)
    aligned, needs_attention, unknown = _bucket(skill_views)
    risks = _interview_risks(db, profile.id)
    progress = _progress(db, goal, skill_views, risks)
    plan = _preparation_plan(goal, needs_attention, unknown, risks, progress)

    jd = db.get(JobDescription, goal.jd_id) if goal.jd_id is not None else None

    return CareerGoalDetailRead(
        id=goal.id,
        title=goal.title,
        company=goal.company,
        location=goal.location,
        job_description=goal.job_description,
        status=goal.status,
        is_analyzed=goal.jd_id is not None,
        created_at=goal.created_at,
        updated_at=goal.updated_at,
        analyzed_at=jd.created_at if jd is not None else None,
        detected_role_title=jd.role_title if jd is not None else None,
        requirements=[
            GoalRequirementRead(
                category=r.category, value=r.value, importance=r.importance, is_explicit=r.is_explicit
            )
            for r in requirements
        ],
        aligned=aligned,
        needs_attention=needs_attention,
        unknown=unknown,
        interview_risks=risks,
        preparation_plan=plan,
        progress=progress,
        next_best_action=_next_best_action(db, profile),
        unavailable_evidence_sources=evidence_engine.unavailable_sources(db, profile.id),
    )


def _requirements_for(db: Session, goal: CareerGoal) -> List[Requirement]:
    if goal.jd_id is None:
        return []
    return (
        db.query(Requirement)
        .filter(Requirement.jd_id == goal.jd_id)
        .order_by(Requirement.id)
        .all()
    )


def _skill_evidence(
    db: Session, profile_id: int, requirements: List[Requirement]
) -> List[GoalSkillEvidenceRead]:
    """Look every skill-shaped requirement up in the Evidence Engine, in one batch.

    One call for the whole list, not one per skill: get_all_skill_evidence fetches this
    profile's evidence rows once and matches every skill against them in memory, which is the
    N+1 its own docstring records being fixed. Calling it per skill here would reintroduce
    exactly that.
    """
    matchable = [r for r in requirements if r.category in _MATCHABLE_CATEGORIES]
    if not matchable:
        return []

    # De-duplicated, because a JD can name the same technology as both a skill and a tool, and
    # asking the engine twice would produce two identical rows in the UI. The first occurrence
    # wins so that a "required" mention is not demoted by a later "preferred" one.
    by_skill: Dict[str, Requirement] = {}
    for requirement in matchable:
        by_skill.setdefault(requirement.value.strip().lower(), requirement)

    ordered = list(by_skill.values())
    views = evidence_engine.get_all_skill_evidence(db, profile_id, [r.value for r in ordered])

    results: List[GoalSkillEvidenceRead] = []
    # strict: one view per skill asked about. A silent length mismatch here would pair a
    # skill with a different skill's evidence, which is the worst failure this file could have.
    for requirement, view in zip(ordered, views, strict=True):
        verdict, label = _EVIDENCE_VERDICTS[view.state]
        results.append(
            GoalSkillEvidenceRead(
                skill=requirement.value,
                importance=requirement.importance,
                is_explicit=requirement.is_explicit,
                evidence_state=view.state,
                evidence_band=view.confidence_band,
                verdict=verdict,
                verdict_label=label,
                evidence_count=len(view.items),
                sources=sorted({item.source for item in view.items}),
                last_verified_at=view.last_verified_at,
            )
        )
    return results


def _bucket(
    views: List[GoalSkillEvidenceRead],
) -> Tuple[List[GoalSkillEvidenceRead], List[GoalSkillEvidenceRead], List[GoalSkillEvidenceRead]]:
    """ALIGNED / NEEDS ATTENTION / UNKNOWN.

    The third bucket is the one that matters most and is easiest to get wrong. A skill with no
    evidence goes to UNKNOWN, not to a "missing skills" list, because this app has not
    established anything about it either way -- it has only established that it has not seen
    anything. Calling that a gap would convert an absence in our records into a claim about the
    candidate.
    """
    aligned = [v for v in views if v.evidence_state in _ALIGNED_STATES]
    needs_attention = [v for v in views if v.verdict == "limited_evidence"]
    unknown = [v for v in views if v.verdict == "no_evidence"]

    # Required before preferred, then explicit before inferred, then alphabetical -- a stable
    # order, so two reads of an unchanged goal never present the same facts differently.
    def _order(item: GoalSkillEvidenceRead):
        return (item.importance != "required", not item.is_explicit, item.skill.lower())

    return sorted(aligned, key=_order), sorted(needs_attention, key=_order), sorted(unknown, key=_order)


def _interview_risks(db: Session, profile_id: int) -> List[GoalInterviewRiskRead]:
    """Preparation areas, from signals that were actually recorded.

    Called with jd_id=None deliberately, which was established by reading what the jd_id
    argument actually does rather than by assuming it was the job-aware option. Passing a
    jd_id narrows every source to rows tied to *that* job description: assessments whose
    blueprint carries the jd_id, interviews whose Interview.jd_id matches, and a JobMatch for
    it. A career goal's parse is new and has none of those by construction, so passing it here
    would reliably return nothing at all -- a screen that says "no preparation areas" to a
    student with a long record of them.

    The general signals are also the right ones. What this section answers is "what has come up
    before that you will want to be ready for", and a weakness in reasoning or project
    ownership is a weakness in any interview, including this one.

    Interview Memory is attached where it exists: an interview-dimension tag that this profile
    has a WeaknessRecord for carries its real occurrence count and status. An absent record
    leaves the fields NULL, never 0 -- a dimension nobody has been graded on has no count.
    """
    items = weakness_service.compute_weaknesses(db, profile_id, None)
    if not items:
        return []

    records = {
        record.skill_key: record
        for record in db.query(WeaknessRecord).filter(WeaknessRecord.profile_id == profile_id).all()
    }

    risks: List[GoalInterviewRiskRead] = []
    for item in items:
        record = records.get(f"{_INTERVIEW_TAG_PREFIX}{item.skill_tag}") or records.get(item.skill_tag)
        risks.append(
            GoalInterviewRiskRead(
                skill_tag=item.skill_tag,
                label=_risk_label(item.skill_tag),
                severity=item.severity,
                sources=item.sources,
                occurrences=record.occurrences if record is not None else None,
                memory_status=record.status if record is not None else None,
            )
        )
    return risks


def _risk_label(skill_tag: str) -> str:
    """Same shaping weakness_record_service._display_label applies, so a tag reads identically
    wherever the student meets it."""
    return skill_tag.removeprefix(_INTERVIEW_TAG_PREFIX).replace("_", " ").capitalize()


# ==========================================================================================
# Deterministic progress
# ==========================================================================================


def _progress(
    db: Session,
    goal: CareerGoal,
    views: List[GoalSkillEvidenceRead],
    risks: List[GoalInterviewRiskRead],
) -> GoalProgressRead:
    """Every field here is counted, not estimated.

    evidence_coverage_percent is the share of this job's matchable skills for which the app has
    seen evidence it considers strong enough to stand on (see _ALIGNED_STATES). It is NULL,
    never 0, when the goal has not been analyzed or its parse produced no matchable skills --
    a percentage computed over an empty set is not a low score, it is not a score.
    """
    total = len(views)
    with_evidence = len([v for v in views if v.evidence_state in _ALIGNED_STATES])
    coverage = round(with_evidence * 100 / total) if total else None

    interviews = (
        db.query(Interview)
        .filter(
            Interview.profile_id == goal.profile_id,
            Interview.status == "completed",
            Interview.created_at >= goal.created_at,
        )
        .count()
    )

    return GoalProgressRead(
        evidence_coverage_percent=coverage,
        skills_total=total,
        skills_with_evidence=with_evidence,
        interviews_since_created=interviews,
        open_risks=len(risks),
        state=_preparation_state(goal, coverage, interviews, len(risks)),
    )


def _preparation_state(
    goal: CareerGoal, coverage: Optional[int], interviews: int, open_risks: int
) -> str:
    """The one-word summary, as an explicit rule rather than a threshold buried in a formula.

    not_analyzed     the job description has never been parsed.
    not_started      parsed, but no interview has been completed since the goal was created.
    ready_for_review evidence covers at least _READY_EVIDENCE_COVERAGE of the job's skills, no
                     open preparation areas remain, and at least one interview has been done.
    in_progress      anything else.

    "ready_for_review" is chosen wording: it says the preparation looks complete enough to go
    over, not that the candidate will succeed. Nothing in this app can know the second thing.
    """
    if goal.jd_id is None:
        return "not_analyzed"
    if interviews == 0:
        return "not_started"
    if coverage is not None and coverage >= _READY_EVIDENCE_COVERAGE and open_risks == 0:
        return "ready_for_review"
    return "in_progress"


# ==========================================================================================
# Preparation plan -- a fixed mapping, not a ranker
# ==========================================================================================


def _preparation_plan(
    goal: CareerGoal,
    needs_attention: List[GoalSkillEvidenceRead],
    unknown: List[GoalSkillEvidenceRead],
    risks: List[GoalInterviewRiskRead],
    progress: GoalProgressRead,
) -> List[GoalPreparationItemRead]:
    """What to do, mapped from what is missing to the feature that already addresses it.

    This is a lookup, not a recommendation engine: each branch below is "if this gap exists,
    here is the existing screen for it". Ranking which of them matters most is
    next_best_action_service's job and is returned alongside, unchanged.

    Every `destination` is a key career_strategy already emits and the Android app already
    resolves through one table, so no item here can point at a screen that does not exist.
    """
    plan: List[GoalPreparationItemRead] = []

    if goal.jd_id is None:
        return [
            GoalPreparationItemRead(
                key="analyze",
                title="Analyze this job description",
                detail=(
                    "Nothing has been read from this posting yet. Analyzing it is what turns it "
                    "into a preparation plan."
                ),
                destination="career_goal_analyze",
            )
        ]

    if unknown:
        skill = unknown[0].skill
        plan.append(
            GoalPreparationItemRead(
                key="build_evidence",
                title=f"Build evidence for {skill}",
                detail=(
                    f"This job asks for {skill}, and nothing in your Skill Passport shows it yet. "
                    "A project or a practice set is the fastest way to change that."
                ),
                destination="project_builder",
                skill=skill,
            )
        )

    if needs_attention:
        skill = needs_attention[0].skill
        plan.append(
            GoalPreparationItemRead(
                key="strengthen_evidence",
                title=f"Strengthen your evidence for {skill}",
                detail=(
                    f"There is something on file for {skill}, but not enough of it to defend in "
                    "an interview. Practice is scored, so it counts as real evidence."
                ),
                destination="practice",
                skill=skill,
            )
        )

    if risks:
        risk = risks[0]
        plan.append(
            GoalPreparationItemRead(
                key="defend_claims",
                title=f"Practise defending {risk.label.lower()}",
                detail=(
                    "Recruiter Attack pushes back on what you claim, which is where this has come "
                    "up before."
                ),
                destination="recruiter_attack",
                skill=risk.skill_tag,
            )
        )

    if progress.interviews_since_created == 0:
        plan.append(
            GoalPreparationItemRead(
                key="full_simulation",
                title="Run a full recruiter simulation for this role",
                detail=(
                    "Twelve questions, from introduction to your own questions at the end -- the "
                    "closest thing here to the real interview."
                ),
                destination="full_simulation",
            )
        )

    if not plan:
        plan.append(
            GoalPreparationItemRead(
                key="keep_sharp",
                title="Keep your evidence current",
                detail=(
                    "Nothing is outstanding for this job right now. Another simulation keeps what "
                    "you have from going stale."
                ),
                destination="full_simulation",
            )
        )
    return plan


def _next_best_action(db: Session, profile: StudentProfile):
    """The existing recommendation, returned as-is.

    Wrapped only so that a failure in an engine this feature merely *displays* cannot take down
    the goal detail the student actually asked for. Broad by intention and narrow in effect: it
    logs the exception type and drops one optional section, and it can never fabricate one,
    because the only alternative to a real recommendation here is None.
    """
    try:
        return next_best_action_service.get_next_best_action(db, profile)
    except Exception as exc:  # noqa: BLE001 -- an optional panel must not fail the page
        logger.warning(
            "Next Best Action unavailable for the career goal detail (%s); omitting the section",
            type(exc).__name__,
        )
        return None


# ==========================================================================================
# List rows
# ==========================================================================================


def _list_item(db: Session, goal: CareerGoal) -> CareerGoalListItem:
    """The list deliberately does NOT compute evidence or risk.

    A list of twenty goals would otherwise mean twenty full Evidence Engine passes on a screen
    that shows one word per row. The one thing it needs beyond the stored columns is whether
    any interview has happened since the goal was created, which is a single count.
    """
    interviews = (
        db.query(Interview)
        .filter(
            Interview.profile_id == goal.profile_id,
            Interview.status == "completed",
            Interview.created_at >= goal.created_at,
        )
        .count()
    )
    # coverage is not computed here, so the list can never show ready_for_review; it stops at
    # in_progress and the detail screen refines it. Under-claiming on a summary row is the safe
    # direction, and it is why the rule lives in one function that takes coverage as an argument
    # rather than being reimplemented per caller.
    state = _preparation_state(goal, None, interviews, 0)
    return CareerGoalListItem(
        id=goal.id,
        title=goal.title,
        company=goal.company,
        location=goal.location,
        status=goal.status,
        is_analyzed=goal.jd_id is not None,
        preparation_state=state,
        created_at=goal.created_at,
        updated_at=goal.updated_at,
    )

