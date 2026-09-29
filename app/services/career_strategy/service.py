"""The unified Career Strategy service -- the one entry point the API calls.

This module is an orchestrator and nothing else. Every number in the response was produced by
the service that owns it: evidence coverage by career_twin_service, the recommendation by
next_best_action_service, evidence by evidence_engine, match components by job_hunt/matching,
interview status by Interview Memory. What happens here is sequencing, composition, persistence
and the premium projection.

**The pipeline runs once per read, in one order.** `signals.collect` is the only function in
the package that touches the database for the strategy itself; everything downstream is pure
over the bundle it returns. That means the expensive part happens once and the rest cannot
introduce an N+1 no matter how it grows.

**The premium split happens here, not in the router and not on the client.** `project` builds a
response with the gated sections genuinely absent. A free client cannot inspect its own traffic
and find the queue, the evidence, or the contradictions in it, because they were never
serialised. `locked_sections` names what is missing so the screen can offer an upgrade instead
of rendering a confusing empty state.

**A snapshot is written only when the decision actually changes.** See snapshots.py for what
"changes" means and why it deliberately excludes wording.

A strategy read used to compute evidence twice -- once for the signal bundle, once inside
career_twin_service for evidence coverage. That cost is gone without moving that work here: the
bundle's evidence views are handed to career_twin_service as a cache, and it still computes
coverage itself, still owning that number alone. Anything it needs that the bundle did not
carry it still fetches, so the optimisation can only make the read cheaper, never different.

No AI. No network. Every decision is deterministic given the rows and the clock.
"""

import dataclasses
import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.placement_strategy_snapshot import PlacementStrategySnapshot
from app.models.profile import StudentProfile
from app.schemas.career_decision import CareerDecisionRoleRead
from app.schemas.career_strategy import (
    LOCKABLE_SECTIONS,
    BottleneckRead,
    CareerStrategyRead,
    ChangedItemRead,
    ContradictionRead,
    DeferredItemRead,
    JobApplicationStateRead,
    JobBlockerRead,
    JobConditionsRead,
    JobStrategyRead,
    PerformanceSeriesRead,
    QueueItemRead,
    ReadinessBiggestGapRead,
    SnapshotSummaryRead,
    StrategyChangeRead,
    StrategyComparisonRead,
    StrategyHistoryRead,
    TargetComparisonRead,
    TargetStrategyRead,
)
from app.schemas.career_twin import CareerTwinRead
from app.services import career_decision_service, career_twin_service, next_best_action_service
from app.services.career_strategy import (
    bottleneck,
    contradictions,
    diminishing_returns,
    gaps,
    job_strategy,
    signals,
    snapshots,
    targets,
)
from app.services.role_context import RoleContext


class SnapshotNotFoundError(LookupError):
    """Raised when a snapshot id does not belong to this student, or does not exist.

    Deliberately one error for both cases. Distinguishing them would let anyone enumerate which
    snapshot ids exist by watching whether they get a 404 or a 403.
    """


@dataclasses.dataclass(frozen=True)
class StrategyView:
    """Everything one strategy read produced, before the premium projection is applied.

    Kept as a dataclass rather than a schema because it holds internal objects (the signal
    bundle, the raw series) that no client ever sees, and because the projection needs the full
    picture in order to decide what to leave out of the response.
    """

    decision: bottleneck.StrategyDecision
    contradictions_found: Tuple[contradictions.StrategyContradiction, ...]
    series: Dict[str, diminishing_returns.PerformanceSeries]
    bundle: signals.SignalBundle
    twin: CareerTwinRead
    snapshot: Optional[PlacementStrategySnapshot]
    snapshot_is_new: bool
    change: Optional[snapshots.StrategyChange]


def compute(
    db: Session,
    profile: StudentProfile,
    *,
    now: Optional[datetime.datetime] = None,
    persist: bool = True,
    role: Optional[RoleContext] = None,
) -> StrategyView:
    """Run the full pipeline once.

    `persist=False` exists for callers that need the decision without touching history -- the
    job-strategy endpoint composes against the current queue and should not write a snapshot as
    a side effect of looking at a job advert.

    `role` runs the identical engine against a different target (see targets.py). Passing it
    forces `persist=False` regardless of what the caller asked for: a snapshot records *the
    student's* strategy, and a hypothetical for a role they have not chosen is not that. Making
    it impossible here rather than asking every call site to remember is the point -- one
    forgetful caller would silently corrupt the history the whole feature is built on.
    """
    if role is not None:
        persist = False

    bundle = signals.collect(db, profile, now=now, role=role)
    gaps_found = gaps.classify(bundle)
    series = diminishing_returns.analyse(bundle)
    contradictions_found = contradictions.detect(bundle, series)

    # Asked, not re-derived. When NBA already has an opinion about this subject, its wording
    # becomes the recommended action so the two screens cannot give different instructions.
    next_action = next_best_action_service.get_next_best_action(db, profile)
    decision = bottleneck.detect(bundle, gaps_found, series, next_action)

    # Career Twin answers "how ready am I for *my* target". For a hypothetical role it would be
    # answering a different question than the one asked, so the comparison path takes its
    # evidence coverage from career_decision_service instead (see targets.py), leaving this empty.
    # The bundle already fetched evidence for a superset of this role's skills, so hand those
    # views over rather than making career_twin_service fetch them again. It still computes
    # coverage -- this is a cache, not a second calculation -- and anything it needs that the
    # bundle did not carry, it still fetches for itself.
    twin = career_twin_service.get_current(
        db, profile, evidence=bundle.evidence_by_skill
    ) if role is None else CareerTwinRead(
        has_role=True, role_source="target_role", display_role_name=role.display_role_name,
        role_family_key=role.role_family_key,
    )

    snapshot: Optional[PlacementStrategySnapshot] = None
    created = False
    change: Optional[snapshots.StrategyChange] = None

    if persist:
        snapshot, created = snapshots.record(db, profile.id, decision, now=bundle.now)
        # Always the row *before* the current one, whether or not this read wrote it. When
        # nothing changed, the honest "what changed" to show is still the last real change --
        # comparing the current snapshot against itself would report a first-ever strategy to a
        # student who has had one for months.
        prior = snapshots.previous_of(db, profile.id, snapshot)
        change = snapshots.explain(
            None if prior is None else prior.decision,
            snapshot.decision,
            from_sequence=None if prior is None else prior.sequence,
            to_sequence=snapshot.sequence,
            from_created_at=None if prior is None else prior.created_at,
            to_created_at=snapshot.created_at,
        )

    return StrategyView(
        decision=decision,
        contradictions_found=contradictions_found,
        series=series,
        bundle=bundle,
        twin=twin,
        snapshot=snapshot,
        snapshot_is_new=created,
        change=change,
    )


def _bottleneck_read(item: bottleneck.Bottleneck, *, is_premium: bool) -> BottleneckRead:
    """The free half is what it is and one thing to do; the Premium half is the working.

    Built by construction rather than by blanking a full object: the gated fields are never
    assigned for a free student, so there is no path by which they can reach the wire.
    """
    free = BottleneckRead(
        id=item.id,
        category=item.category,
        subject=item.subject,
        subject_kind=item.subject_kind,
        title=item.title,
        destination=item.destination,
        recommended_action=item.recommended_action,
        strategy_version=item.strategy_version,
    )
    if not is_premium:
        return free

    return free.model_copy(
        update={
            "description": item.description,
            "severity": item.severity,
            "impact": item.impact,
            "effort": item.effort,
            "confidence": item.confidence,
            "evidence": list(item.evidence),
            "affected_roles": list(item.affected_roles),
            "affected_job_ids": list(item.affected_job_ids),
            "reason_code": item.reason_code,
            "created_from_signals": list(item.created_from_signals),
            "decision_signals": list(item.decision_signals),
        }
    )


def _changed_item(item: snapshots.ChangedItem) -> ChangedItemRead:
    return ChangedItemRead(id=item.id, category=item.category, subject=item.subject, title=item.title)


def _change_read(change: snapshots.StrategyChange) -> StrategyChangeRead:
    return StrategyChangeRead(
        changed=change.changed,
        from_sequence=change.from_sequence,
        to_sequence=change.to_sequence,
        from_created_at=change.from_created_at,
        to_created_at=change.to_created_at,
        state_from=change.state_from,
        state_to=change.state_to,
        state_changed=change.state_changed,
        bottleneck_changed=change.bottleneck_changed,
        bottleneck_from=None if change.bottleneck_from is None else _changed_item(change.bottleneck_from),
        bottleneck_to=None if change.bottleneck_to is None else _changed_item(change.bottleneck_to),
        cleared=[_changed_item(entry) for entry in change.cleared],
        displaced=[_changed_item(entry) for entry in change.displaced],
        added=[_changed_item(entry) for entry in change.added],
        still_open=[_changed_item(entry) for entry in change.still_open],
        spans_version_change=change.spans_version_change,
        version_from=change.version_from,
        version_to=change.version_to,
        summary=list(change.summary),
    )


def _series_reads(
    series: Dict[str, diminishing_returns.PerformanceSeries]
) -> List[PerformanceSeriesRead]:
    """Only subjects with enough history to say anything.

    A series below MIN_OBSERVATIONS has every statistic as None by design, and listing a dozen
    of those would fill the section with rows that answer nothing. The question this section
    exists to answer -- where is more of the same no longer paying off -- can only be answered
    where a real history exists.
    """
    return [
        PerformanceSeriesRead(
            subject=entry.subject,
            verdict=entry.verdict,
            observation_count=entry.observation_count,
            is_sufficient=entry.is_sufficient,
            mean=entry.mean,
            variance=entry.variance,
            trend=entry.trend,
            latest=entry.latest,
            reason=entry.reason,
        )
        for _key, entry in sorted(series.items())
        if entry.is_sufficient
    ]


def project(view: StrategyView, *, is_premium: bool) -> CareerStrategyRead:
    """Build the response, including only what this student is entitled to.

    Server-authoritative by construction: the gated collections are built solely inside the
    `is_premium` branch. There is no full response that later gets trimmed, and therefore no
    version of this function that can be made to leak by a later refactor moving a line.
    """
    decision = view.decision
    role = view.bundle.role

    response = CareerStrategyRead(
        is_premium=is_premium,
        locked_sections=[] if is_premium else list(LOCKABLE_SECTIONS),
        state=decision.state,
        state_reason=decision.state_reason,
        strategy_version=decision.strategy_version,
        has_role=role is not None,
        role_family_key=None if role is None else role.role_family_key,
        display_role_name=None if role is None else role.display_role_name,
        role_source="none" if role is None else role.role_source,
        role_is_estimate=False if role is None else role.is_estimate,
        evidence_coverage_percent=view.twin.evidence_coverage_percent,
        evidence_coverage_is_estimate=view.twin.is_estimate,
        evidence_coverage_reasons=list(view.twin.reasons),
        signal_count=len(view.bundle.signals),
        bottleneck=(
            None
            if decision.primary is None
            else _bottleneck_read(decision.primary, is_premium=is_premium)
        ),
        snapshot_id=None if view.snapshot is None else view.snapshot.id,
        snapshot_sequence=None if view.snapshot is None else view.snapshot.sequence,
        snapshot_created_at=None if view.snapshot is None else view.snapshot.created_at,
        snapshot_is_new=view.snapshot_is_new,
    )

    if not is_premium:
        return response

    return response.model_copy(
        update={
            "queue": [
                QueueItemRead(
                    id=item.id,
                    category=item.category,
                    subject=item.subject,
                    title=item.title,
                    impact=item.impact,
                    effort=item.effort,
                    destination=item.destination,
                    reason=item.reason,
                    status=item.status,
                )
                for item in decision.queue
            ],
            "do_not_do_yet": [
                DeferredItemRead(
                    id=item.id,
                    category=item.category,
                    subject=item.subject,
                    title=item.title,
                    reason=item.reason,
                    instead=item.instead,
                )
                for item in decision.do_not_do_yet
            ],
            "contradictions": [
                ContradictionRead(
                    kind=item.kind,
                    subject=item.subject,
                    left_statement=item.left_statement,
                    left_signal_keys=list(item.left_signal_keys),
                    right_statement=item.right_statement,
                    right_signal_keys=list(item.right_signal_keys),
                    explanation=item.explanation,
                    reclassify_to=item.reclassify_to,
                )
                for item in view.contradictions_found
            ],
            "diminishing_returns": _series_reads(view.series),
            "latest_change": None if view.change is None else _change_read(view.change),
        }
    )


def get_strategy(
    db: Session,
    profile: StudentProfile,
    *,
    is_premium: bool,
    now: Optional[datetime.datetime] = None,
) -> CareerStrategyRead:
    """The whole screen, entitlement applied."""
    return project(compute(db, profile, now=now), is_premium=is_premium)


# -- history ----------------------------------------------------------------------------------


def _summary(row: PlacementStrategySnapshot) -> SnapshotSummaryRead:
    return SnapshotSummaryRead(
        id=row.id,
        sequence=row.sequence,
        state=row.state,
        strategy_version=row.strategy_version,
        primary_bottleneck_id=row.primary_bottleneck_id,
        primary_category=row.primary_category,
        primary_subject=row.primary_subject,
        created_at=row.created_at,
    )


def get_history(
    db: Session, profile_id: int, limit: int = snapshots.DEFAULT_HISTORY_LIMIT
) -> StrategyHistoryRead:
    """Newest first. Every row here is a point at which the strategy genuinely moved -- opening
    the screen ten times does not add ten entries.

    Each entry carries its own "what changed" lines, diffed against the snapshot below it in
    the same page. That costs nothing extra -- the rows are already in memory, and one extra
    row is fetched as the baseline for the oldest entry -- and it is what lets the history
    screen render from a single request instead of one /compare call per row.
    """
    rows, baseline = snapshots.history_with_baseline(db, profile_id, limit)

    entries: List[SnapshotSummaryRead] = []
    for index, row in enumerate(rows):
        # Rows are newest-first, so the predecessor of row[i] is row[i + 1] -- falling off the
        # end of the page onto the extra baseline row we deliberately fetched.
        prior = rows[index + 1] if index + 1 < len(rows) else baseline
        change = snapshots.explain(
            None if prior is None else prior.decision,
            row.decision,
            from_sequence=None if prior is None else prior.sequence,
            to_sequence=row.sequence,
        )
        entries.append(_summary(row).model_copy(update={"change_summary": list(change.summary)}))

    return StrategyHistoryRead(snapshots=entries)


def compare(db: Session, profile_id: int, snapshot_id: int) -> StrategyComparisonRead:
    """One snapshot against the one immediately before it.

    Raises SnapshotNotFoundError if the snapshot is not this student's -- see that class for why
    it does not distinguish "missing" from "not yours".
    """
    row = snapshots.get(db, profile_id, snapshot_id)
    if row is None:
        raise SnapshotNotFoundError(f"Snapshot {snapshot_id} not found")

    previous = snapshots.previous_of(db, profile_id, row)
    change = snapshots.explain(
        None if previous is None else previous.decision,
        row.decision,
        from_sequence=None if previous is None else previous.sequence,
        to_sequence=row.sequence,
        from_created_at=None if previous is None else previous.created_at,
        to_created_at=row.created_at,
    )
    return StrategyComparisonRead(
        snapshot=_summary(row),
        compared_to=None if previous is None else _summary(previous),
        change=_change_read(change),
    )


# -- job-specific -------------------------------------------------------------------------------


def _biggest_gap_read(raw: Optional[Dict[str, Any]]) -> Optional[ReadinessBiggestGapRead]:
    """readiness.py's dict -> the wire object. Returns None for anything malformed rather than
    a half-filled card: a gap with no title is not a smaller gap, it is a bug, and rendering it
    would put an empty "do this next" box in front of the student."""
    if not raw:
        return None
    title = raw.get("title")
    detail = raw.get("detail")
    kind = raw.get("kind")
    action = raw.get("action")
    if not (title and detail and kind and action):
        return None
    skill = raw.get("skill")
    return ReadinessBiggestGapRead(
        kind=str(kind),
        skill=None if skill is None else str(skill),
        title=str(title),
        detail=str(detail),
        action=str(action),
    )


def get_job_strategy(
    db: Session,
    profile: StudentProfile,
    listing_id: int,
    *,
    now: Optional[datetime.datetime] = None,
) -> JobStrategyRead:
    """One advert, judged against what this student can actually show.

    The career-level decision is computed with `persist=False`: looking at a job is not a
    strategic event, and writing a history entry for it would fill the timeline with rows the
    student did not cause.
    """
    view = compute(db, profile, now=now, persist=False)
    result = job_strategy.compose(
        db, profile, listing_id, career_decision=view.decision, now=now
    )
    return JobStrategyRead(
        listing_id=result.listing_id,
        job_title=result.job_title,
        company=result.company,
        decision=result.decision,
        headline=result.headline,
        reasons=list(result.reasons),
        match_score=result.match_score,
        score_version=result.score_version,
        component_scores=dict(result.component_scores),
        match_rationale=result.match_rationale,
        unknown_factors=list(result.unknown_factors),
        deciding_factors=list(result.deciding_factors),
        application=JobApplicationStateRead(
            is_saved=result.application.is_saved,
            applied_at=result.application.applied_at,
            status=result.application.status,
        ),
        conditions=JobConditionsRead(
            location=result.conditions.location,
            work_mode=result.conditions.work_mode,
            employment_type=result.conditions.employment_type,
            salary_min=result.conditions.salary_min,
            salary_max=result.conditions.salary_max,
            salary_currency=result.conditions.salary_currency,
            salary_period=result.conditions.salary_period,
            salary_is_predicted=result.conditions.salary_is_predicted,
        ),
        readiness_percent=result.readiness_percent,
        readiness_band=result.readiness_band,
        judged_dimensions=result.judged_dimensions,
        unknown_dimensions=list(result.unknown_dimensions),
        biggest_gap=_biggest_gap_read(result.biggest_gap),
        verified_skill_count=result.verified_skill_count,
        missing_skills=list(result.missing_skills),
        unproven_skills=list(result.unproven_skills),
        unclear_skills=list(result.unclear_skills),
        blockers=[
            JobBlockerRead(
                kind=item.kind,
                subject=item.subject,
                title=item.title,
                detail=item.detail,
                destination=item.destination,
            )
            for item in result.blockers
        ],
        prep_from_career_strategy=list(result.prep_from_career_strategy),
    )


# -- multi-target -------------------------------------------------------------------------------


def _tradeoffs(entries: List[TargetStrategyRead], shared_skills: List[str]) -> List[str]:
    """Plain statements of the actual trade, derived only from numbers already computed.

    Every line has to be something a student could verify by reading the table above it. No line
    recommends a role: which career to pursue is not a decision this system is entitled to make,
    and a "you should pick X" built on skill weights would be exactly the confident-sounding
    overreach the rest of this feature refuses.
    """
    if len(entries) < 2:
        return []

    lines: List[str] = []
    ranked = [entry for entry in entries if entry.evidence_coverage_percent is not None]
    ranked.sort(key=lambda entry: -(entry.evidence_coverage_percent or 0))

    if len(ranked) >= 2:
        best, second = ranked[0], ranked[1]
        if best.evidence_coverage_percent != second.evidence_coverage_percent:
            lines.append(
                f"You are closer to {best.display_role_name} ({best.evidence_coverage_percent}%) than to "
                f"{second.display_role_name} ({second.evidence_coverage_percent}%) on the evidence you have today."
            )
        else:
            lines.append(
                f"{best.display_role_name} and {second.display_role_name} are equally far along on "
                "the evidence you have today; what separates them is the work each still needs."
            )

    if shared_skills:
        listed = ", ".join(shared_skills[:4])
        lines.append(
            f"Work on {listed} counts toward more than one of these targets, so it is the least "
            "wasted effort whichever way you go."
        )

    for entry in [entry for entry in entries if entry.unique_gaps][:2]:
        listed = ", ".join(entry.unique_gaps[:3])
        lines.append(f"Choosing {entry.display_role_name} specifically adds: {listed}.")

    blockers = {entry.bottleneck_category for entry in entries if entry.bottleneck_category}
    if len(entries) > 1 and len(blockers) == 1:
        only = next(iter(blockers))
        # Only say this for constraints that genuinely are role-independent. A SKILL_GAP shared
        # across targets is shared because the roles overlap, which is a different statement.
        if only in ("INTERVIEW_GAP", "RESUME_GAP", "APPLICATION_GAP", "PROFILE_GAP"):
            lines.append(
                "The same thing is holding you back for every target here, so it is worth fixing "
                "before the choice between them matters."
            )

    return lines


def _primary_role_id(primary: Optional[RoleContext]) -> Optional[int]:
    """The catalogue role id behind the student's real target, when there is one.

    Career Paths use their own family keys and have no Role row, so this is None for them --
    which means career_decision_service cannot score that target and its coverage column stays
    empty rather than being filled with a number from somewhere else.
    """
    if primary is None or not primary.role_family_key.startswith("role:"):
        return None
    try:
        return int(primary.role_family_key.split(":", 1)[1])
    except ValueError:
        return None


def compare_targets(
    db: Session,
    profile: StudentProfile,
    *,
    is_premium: bool,
    now: Optional[datetime.datetime] = None,
) -> TargetComparisonRead:
    """Run the one strategy engine against each saved target and put the results side by side.

    Evidence coverage, classification and effort come from career_decision_service in one call --
    the same numbers Career Decision Lab shows for the same role, not a second opinion. The
    state, bottleneck and top action for each come from this package's own engine with the role
    context swapped, so a comparison column is produced by exactly the code that produces the
    main screen.

    Free callers get the shape and nothing in it: comparison is Premium, and `locked` tells the
    client to show an upgrade prompt rather than an empty table.
    """
    if not is_premium:
        return TargetComparisonRead(
            is_premium=False, locked=True, max_targets=targets.MAX_COMPARISON_TARGETS
        )

    primary, saved = targets.comparison_set(db, profile)

    contexts: List[Tuple[Optional[int], RoleContext, bool]] = []
    if primary is not None:
        contexts.append((_primary_role_id(primary), primary, True))
    for role, context in saved:
        if primary is not None and context.role_family_key == primary.role_family_key:
            continue  # already present as the current target; never listed twice
        contexts.append((role.id, context, False))

    if not contexts:
        return TargetComparisonRead(is_premium=True, max_targets=targets.MAX_COMPARISON_TARGETS)

    # One call for every role: per-role evidence coverage and the shared-skill analysis in one pass
    # through the existing engine, rather than one simulation per target.
    scored: Dict[int, CareerDecisionRoleRead] = {}
    shared_skills: List[str] = []
    assumptions: List[str] = []
    scorable = [role_id for role_id, _context, _current in contexts if role_id is not None]
    if scorable:
        simulation = career_decision_service.simulate(db, profile.id, scorable, None)
        scored = {row.role_id: row for row in simulation.roles}
        shared_skills = list(simulation.shared_skills)
        assumptions = list(simulation.assumptions)

    # Every target's decision first, so "unique to this target" is decided across the whole set
    # rather than guessed at one column at a time.
    decisions: List[Tuple[Optional[int], RoleContext, bool, bottleneck.StrategyDecision]] = []
    queued_subjects: List[List[str]] = []
    for role_id, context, is_current in contexts:
        view = compute(db, profile, now=now, role=context)
        decisions.append((role_id, context, is_current, view.decision))
        queued_subjects.append([item.subject for item in view.decision.queue])

    counts: Dict[str, int] = {}
    for subjects in queued_subjects:
        for subject in set(subjects):
            counts[subject] = counts.get(subject, 0) + 1

    entries: List[TargetStrategyRead] = []
    for index, (role_id, context, is_current, decision) in enumerate(decisions):
        row = scored.get(role_id) if role_id is not None else None
        subjects = queued_subjects[index]
        top = decision.primary
        entries.append(
            TargetStrategyRead(
                role_id=role_id,
                display_role_name=context.display_role_name,
                is_current_target=is_current,
                # Career Decision's evidence-coverage number. Phase 2G gave this field the
                # name its value has always had; it was already Optional and already rendered
                # honestly when null, so an unmeasurable role flows through as null end to end
                # rather than arriving as a confident 0.
                evidence_coverage_percent=None if row is None else row.evidence_coverage_percent,
                classification=None if row is None else row.classification,
                estimated_hours_to_strengthen=(
                    None if row is None else row.estimated_hours_to_strengthen
                ),
                state=decision.state,
                bottleneck_title=None if top is None else top.title,
                bottleneck_category=None if top is None else top.category,
                top_action=None if top is None else top.recommended_action,
                top_action_destination=None if top is None else top.destination,
                impact=None if top is None else top.impact,
                effort=None if top is None else top.effort,
                unique_gaps=sorted({s for s in subjects if counts.get(s, 0) == 1}),
                shared_gaps=sorted({s for s in subjects if counts.get(s, 0) > 1}),
            )
        )

    return TargetComparisonRead(
        is_premium=True,
        targets=entries,
        shared_skills=shared_skills,
        tradeoffs=_tradeoffs(entries, shared_skills),
        assumptions=assumptions,
        max_targets=targets.MAX_COMPARISON_TARGETS,
    )
