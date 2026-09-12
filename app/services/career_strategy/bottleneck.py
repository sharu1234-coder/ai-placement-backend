"""Bottleneck detection -- the single constraint most worth removing right now.

This is the question the whole feature exists to answer, and the one that is easiest to answer
badly. The naive version picks the lowest number on the screen. That reliably tells a student
with strong code, a strong resume and a weak interview to go learn another framework, because
"another framework" is always the easiest thing to generate a task for.

So the bottleneck is not the lowest score. It is the highest-priority *gap*, where priority is
the explainable product of role relevance, severity, evidence deficit, real demand from the
student's own tracked jobs, diminishing returns on their own measured history, and whether the
thing can actually be acted on (see prioritization.py). Strengths matter here as much as
weaknesses: a skill the student has already proved and kept steady is damped hard, precisely so
it stops crowding out the thing that is actually holding them back.

**Composition, not competition.** The recommended action comes from
next_best_action_service where that service already has an opinion about this subject. Career
Strategy does not run a second recommendation ladder alongside the existing one and hope they
agree -- it asks, and falls back to a category-level action only when NBA is talking about
something else.

**Nothing here recomputes anything.** Evidence confidence is evidence_engine's. Match
components are job_hunt/matching's. Interview status is Interview Memory's. This module ranks
and explains; every underlying number was produced by the service that owns it.

Deterministic throughout: the same bundle and the same gaps produce the same bottleneck, the
same queue and the same ordering, every time.
"""

import re
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

from app.schemas.next_best_action import NextBestActionRead
from app.services.career_strategy import prioritization
from app.services.career_strategy.diminishing_returns import PerformanceSeries
from app.services.career_strategy.gaps import StrategyGap
from app.services.career_strategy.signals import SignalBundle

# Bumped only when the ranking logic materially changes. Snapshots keep the version that
# produced them so a historical decision is never silently reinterpreted under new rules.
#
# v2 replaced a severity-shaped state ladder (NEEDS_ATTENTION / BLOCKED / ON_TRACK) with the
# focus-shaped one below. The old names told a student how worried to be; these tell them which
# part of their career to work on, which is the question the screen exists to answer. History
# spanning the bump says so out loud rather than presenting it as the student having moved --
# see snapshots.explain.
STRATEGY_VERSION = 2

STRATEGY_STATES: Tuple[str, ...] = (
    "INSUFFICIENT_INFORMATION",
    "FOCUS_ON_TARGET_ROLE",
    "FOCUS_ON_SKILLS",
    "FOCUS_ON_EVIDENCE",
    "FOCUS_ON_INTERVIEW",
    "FOCUS_ON_RESUME",
    "FOCUS_ON_APPLICATION_STRATEGY",
    "FOCUS_ON_PROFILE",
    "FOCUS_ON_EXPERIENCE",
    "READY_TO_APPLY",
)

# The state is the bottleneck's category, said in the student's terms. One table, no second
# ranking pass: whatever the engine decided is the primary constraint decides the state, so the
# headline and the card beneath it can never disagree.
#
# JOB_TARGETING_GAP and APPLICATION_GAP share a state deliberately. Targeting the wrong jobs and
# not applying to enough of them are both failures of application strategy, and splitting them
# would give the screen two headlines for one problem.
_STATE_BY_CATEGORY: Dict[str, str] = {
    "SKILL_GAP": "FOCUS_ON_SKILLS",
    "EVIDENCE_GAP": "FOCUS_ON_EVIDENCE",
    "INTERVIEW_GAP": "FOCUS_ON_INTERVIEW",
    "RESUME_GAP": "FOCUS_ON_RESUME",
    "APPLICATION_GAP": "FOCUS_ON_APPLICATION_STRATEGY",
    "JOB_TARGETING_GAP": "FOCUS_ON_APPLICATION_STRATEGY",
    "EXPERIENCE_GAP": "FOCUS_ON_EXPERIENCE",
    "PROFILE_GAP": "FOCUS_ON_PROFILE",
}

_STATE_REASONS: Dict[str, str] = {
    "FOCUS_ON_SKILLS": "A skill your target role depends on is the thing most worth working on now.",
    "FOCUS_ON_EVIDENCE": "You have the skills; what is missing is independent proof of them.",
    "FOCUS_ON_INTERVIEW": "Your technical side is holding up. How you come across in an interview is not.",
    "FOCUS_ON_RESUME": "What you can already prove is not reaching your resume.",
    "FOCUS_ON_APPLICATION_STRATEGY": "The work is in how and where you are applying, not in what you can do.",
    "FOCUS_ON_EXPERIENCE": (
        "Your largest constraint is experience, which is not something you can change this month. "
        "The queue below is what is worth doing meanwhile."
    ),
    "FOCUS_ON_PROFILE": "Filling in the rest of your profile is what unlocks a sharper strategy.",
}

QUEUE_STATUSES: Tuple[str, ...] = ("RECOMMENDED", "IN_PROGRESS", "BLOCKED", "LOWER_PRIORITY")

# Below this many independently-observed signals there is not enough to rank honestly. A single
# assessment does not make a strategy, and presenting one as though it did would give a student
# a confident-looking answer built on one data point.
MIN_SIGNALS_FOR_STRATEGY = 3

# How many items the queue and the deferred list carry. Long lists are the task-dump failure
# mode this feature exists to avoid: the point is a decision, not an inventory.
MAX_QUEUE_ITEMS = 5
MAX_DEFERRED_ITEMS = 3

_HIGH_IMPACT = ("high", "very_high")

# Human phrasing per category. Kept as a table rather than assembled ad hoc at each call site so
# the same gap always reads the same way, and so the wording can be reviewed in one place.
_TITLES: Dict[str, str] = {
    "SKILL_GAP": "{subject}",
    "EVIDENCE_GAP": "Proof for {subject}",
    "EXPERIENCE_GAP": "Experience level",
    "RESUME_GAP": "{subject} on your resume",
    "INTERVIEW_GAP": "Interview {label}",
    "APPLICATION_GAP": "Your application funnel",
    "JOB_TARGETING_GAP": "What you are targeting",
    "PROFILE_GAP": "Your profile",
}

_ACTIONS: Dict[str, str] = {
    "EVIDENCE_GAP": "Build one piece of independent proof for {subject}",
    "EXPERIENCE_GAP": "Compare roles that match your current experience level",
    "RESUME_GAP": "Add {subject} to your resume, backed by the evidence you already have",
    "INTERVIEW_GAP": "Run a mock interview focused on {label}",
    "APPLICATION_GAP": "Review your application funnel",
    "JOB_TARGETING_GAP": "Refine what you are searching for",
    "PROFILE_GAP": "Complete {subject}",
}

_SKILL_ACTIONS = {
    "knowledge": "Start learning {subject}",
    "ability": "Practise {subject}",
}


class Bottleneck(BaseModel):
    """The constraint currently most worth removing, with its full working attached."""

    model_config = ConfigDict(frozen=True)

    # Stable and derived from what the bottleneck *is*, not from row order -- so a snapshot
    # taken on Monday and one taken on Wednesday can be compared by id to answer "did this
    # actually change?" without string-matching a title someone may have reworded.
    id: str
    category: str
    subject: str
    subject_kind: str
    title: str
    description: str
    severity: str
    impact: str
    effort: str
    # How much this conclusion can be leaned on, given how much we could see. Never a
    # percentage: it is a band, and it exists so a thin profile does not produce a
    # confident-sounding answer.
    confidence: str
    evidence: List[str] = []
    affected_roles: List[str] = []
    affected_job_ids: List[int] = []
    recommended_action: str
    destination: str
    created_from_signals: List[str] = []
    reason_code: str
    decision_signals: List[str] = []
    strategy_version: int = STRATEGY_VERSION


class QueueItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    category: str
    subject: str
    title: str
    impact: str
    effort: str
    destination: str
    reason: str
    status: str = "RECOMMENDED"


class DeferredItem(BaseModel):
    """Something worth doing eventually, that is not the best use of this week.

    Carries `instead` so the screen can say what to do with the time rather than only what not
    to do -- "don't polish your resume" on its own is not advice.

    `category` is None for the strength case. That case has no gap behind it by construction: a
    skill the student has genuinely mastered produces no gap at all, which is exactly why it
    needs saying out loud. "You already have strong SQL evidence, another SQL course will not
    move your bottleneck" is advice about something the gap list cannot contain.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    category: Optional[str]
    subject: str
    title: str
    reason: str
    instead: Optional[str] = None


class StrategyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: str
    primary: Optional[Bottleneck] = None
    queue: List[QueueItem] = []
    do_not_do_yet: List[DeferredItem] = []
    strategy_version: int = STRATEGY_VERSION
    # Why the state is what it is, in the same build-it-from-the-signals way as everything else.
    state_reason: str = ""


def _slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _gap_id(gap: StrategyGap) -> str:
    return f"{gap.category}:{gap.subject}"


def _dimension_label(subject: str) -> str:
    return subject.split(":")[-1].replace("_", " ")


def _title_for(gap: StrategyGap) -> str:
    return _TITLES[gap.category].format(subject=gap.subject, label=_dimension_label(gap.subject))


def _action_for(gap: StrategyGap) -> str:
    if gap.category == "SKILL_GAP":
        return _SKILL_ACTIONS[gap.sub_kind or "knowledge"].format(subject=gap.subject)
    return _ACTIONS[gap.category].format(subject=gap.subject, label=_dimension_label(gap.subject))


def _confidence_band(bundle: SignalBundle, gap: StrategyGap) -> str:
    """How much this conclusion can carry, from how much we could actually see.

    Counts only independently-observed signals -- claims do not raise confidence in a
    conclusion any more than they raise confidence in a skill.
    """
    observed = sum(1 for s in bundle.signals if s.evidence_kind in ("observed", "verified"))
    supporting = len(gap.signal_keys)
    if observed >= 8 and supporting >= 2:
        return "high"
    if observed >= MIN_SIGNALS_FOR_STRATEGY:
        return "moderate"
    return "low"


def _affected_job_ids(bundle: SignalBundle, gap: StrategyGap) -> List[int]:
    """Tracked listings this gap actually bears on, by name.

    Only listings whose authoritative match row names the subject as missing. Never "jobs like
    this" or a similarity guess -- if the match engine did not say the skill was missing for
    that advert, this does not claim it was.
    """
    if gap.subject_kind != "skill":
        return []
    ids = [
        listing.id
        for listing, match in bundle.tracked_jobs
        if match is not None and gap.subject in (match.missing_skills or [])
    ]
    return sorted(ids)


def _evidence_lines(gap: StrategyGap, priority: prioritization.Priority, nba_reasons: List[str]) -> List[str]:
    """The "why this?" bullets. Built from the deciding factors and, where NBA is talking about
    the same subject, from its own already-deterministic reasons. Never generated from the
    conclusion after the fact."""
    lines = [gap.reason]
    lines.extend(priority.factors.notes)
    for reason in nba_reasons:
        if reason not in lines:
            lines.append(reason)
    return lines


def _state(
    bundle: SignalBundle, ranked: List[Tuple[StrategyGap, prioritization.Priority]]
) -> Tuple[str, str]:
    """Which part of their career this student should be working on, and why.

    Deterministic from the signals in three steps, in this order: is there a target to measure
    against, is there enough to measure, and then -- the whole point -- which category did the
    engine decide is the constraint. Nothing here re-ranks anything; the state is the primary
    bottleneck's category restated, so the headline can never contradict the card below it.
    """
    if bundle.role is None:
        return "FOCUS_ON_TARGET_ROLE", "Choose a target role and the rest of your strategy follows from it."

    observed = sum(1 for s in bundle.signals if s.evidence_kind in ("observed", "verified"))
    if observed < MIN_SIGNALS_FOR_STRATEGY:
        return (
            "INSUFFICIENT_INFORMATION",
            "Complete more of your profile, evidence or practice activity to generate a stronger strategy.",
        )

    if not ranked:
        return "READY_TO_APPLY", "No open gaps against your target role right now."

    top_gap, _top_priority = ranked[0]
    state = _STATE_BY_CATEGORY[top_gap.category]
    return state, _STATE_REASONS[state]


def _deferred(
    ranked: List[Tuple[StrategyGap, prioritization.Priority]],
    series: Dict[str, PerformanceSeries],
    primary_title: Optional[str],
) -> List[DeferredItem]:
    """Work that is valid eventually and is not the best use of this week.

    Three independent reasons qualify, all from real signals rather than a rule of thumb.

    The first is the strength case, and it is the one the gap list structurally cannot produce:
    a skill the student has already mastered generates no gap, so without this the engine could
    never say "you have strong SQL evidence already; another SQL course will not move your
    bottleneck" -- which is the single most useful thing it can tell a student who keeps
    returning to their comfort zone. It is drawn straight from their own measured series, so it
    only fires once there is enough history to be sure.

    The other two are gap-derived: the subject's history says more of the same buys little, or
    the gap is genuinely low impact while something high-impact is open.

    Never "this is useless" -- always "something else is worth more right now", with the
    something else named.
    """
    deferred: List[DeferredItem] = []
    covered: set = set()

    # Strengths first: these are the ones a student is most likely to over-invest in, precisely
    # because they are pleasant and produce visible activity.
    for subject in sorted(series):
        if len(deferred) >= MAX_DEFERRED_ITEMS:
            break
        entry = series[subject]
        if not entry.is_sufficient or entry.verdict != "stable_high":
            continue
        covered.add(subject)
        deferred.append(
            DeferredItem(
                id=f"STRENGTH:{subject}",
                category=None,
                subject=subject,
                title=f"More practice on {subject}",
                reason=entry.reason,
                instead=(f"Focus on {primary_title} first." if primary_title else None),
            )
        )

    if not ranked:
        return deferred

    top_impact = ranked[0][1].impact_band
    for gap, priority in ranked:
        if len(deferred) >= MAX_DEFERRED_ITEMS:
            break
        if _title_for(gap) == primary_title or gap.subject in covered:
            continue

        entry = series.get(gap.subject)
        if entry is not None and entry.is_sufficient and entry.verdict in ("stable_high", "improving"):
            reason = entry.reason
        elif priority.impact_band == "low" and top_impact in _HIGH_IMPACT:
            reason = "Worth doing eventually, but lower impact than what is currently open."
        else:
            continue

        covered.add(gap.subject)
        deferred.append(
            DeferredItem(
                id=_gap_id(gap),
                category=gap.category,
                subject=gap.subject,
                title=_action_for(gap),
                reason=reason,
                instead=(f"Focus on {primary_title} first." if primary_title else None),
            )
        )
    return deferred


def detect(
    bundle: SignalBundle,
    gaps_found: Tuple[StrategyGap, ...],
    series: Dict[str, PerformanceSeries],
    next_best_action: Optional[NextBestActionRead] = None,
) -> StrategyDecision:
    """Rank every gap, name the bottleneck, build the queue and the deferred list.

    `next_best_action` is the existing service's output, passed in rather than called so this
    stays pure and so the composition is visible at the call site. When it names the same
    subject as the bottleneck, its title becomes the recommended action and its reasons join
    the evidence -- one recommendation, from the service that already owns recommendations.
    """
    scored: List[Tuple[StrategyGap, prioritization.Priority]] = []
    for gap in gaps_found:
        view = bundle.evidence_by_skill.get(gap.subject) if gap.subject_kind == "skill" else None
        confidence = None
        if view is not None and view.state != "claimed":
            confidence = view.confidence
        scored.append(
            (gap, prioritization.score(gap, series=series.get(gap.subject), evidence_confidence=confidence))
        )

    ranked = prioritization.rank(scored)
    state, state_reason = _state(bundle, ranked)

    if state in ("FOCUS_ON_TARGET_ROLE", "INSUFFICIENT_INFORMATION"):
        return StrategyDecision(state=state, state_reason=state_reason)

    if not ranked:
        # No open gaps. Strengths worth not over-investing in are still worth naming -- that is
        # the whole "don't do this yet" case for a student who is already in good shape.
        return StrategyDecision(
            state=state,
            do_not_do_yet=_deferred(ranked, series, None),
            state_reason=state_reason,
        )

    top_gap, top_priority = ranked[0]

    nba_matches = (
        next_best_action is not None
        and next_best_action.has_recommendation
        and next_best_action.skill is not None
        and next_best_action.skill == top_gap.subject
    )
    recommended_action = next_best_action.title if nba_matches else _action_for(top_gap)
    nba_reasons = list(next_best_action.reasons) if nba_matches else []

    primary = Bottleneck(
        id=_gap_id(top_gap),
        category=top_gap.category,
        subject=top_gap.subject,
        subject_kind=top_gap.subject_kind,
        title=_title_for(top_gap),
        description=top_gap.reason,
        severity=top_gap.severity,
        impact=top_priority.impact_band,
        effort=top_priority.effort_band,
        confidence=_confidence_band(bundle, top_gap),
        evidence=_evidence_lines(top_gap, top_priority, nba_reasons),
        affected_roles=[bundle.role.display_role_name] if bundle.role is not None else [],
        affected_job_ids=_affected_job_ids(bundle, top_gap),
        recommended_action=recommended_action,
        destination=top_gap.destination,
        created_from_signals=list(top_gap.signal_keys),
        reason_code=f"{top_gap.category}__{_slug(top_gap.subject)}",
        decision_signals=sorted({key for gap, _p in ranked[:MAX_QUEUE_ITEMS] for key in gap.signal_keys}),
        strategy_version=STRATEGY_VERSION,
    )

    queue = [
        QueueItem(
            id=_gap_id(gap),
            category=gap.category,
            subject=gap.subject,
            title=_action_for(gap),
            impact=priority.impact_band,
            effort=priority.effort_band,
            destination=gap.destination,
            reason=gap.reason,
            status="BLOCKED" if priority.factors.actionability < 1.0 else "RECOMMENDED",
        )
        for gap, priority in ranked[:MAX_QUEUE_ITEMS]
    ]

    return StrategyDecision(
        state=state,
        primary=primary,
        queue=queue,
        do_not_do_yet=_deferred(ranked, series, primary.title),
        strategy_version=STRATEGY_VERSION,
        state_reason=state_reason,
    )
