"""VA-8: the coaching layer. Structured findings and plans, built from evidence.

Why this is code and not a prompt. A coaching answer makes claims about a student's prospects,
and the three things that must hold about it -- that every statement names its support, that a
recommendation never reads as a fact, and that nothing promises an outcome -- are properties of
a data structure or they are nothing. Expressed as instructions in a prompt they hold until the
model has a bad day; expressed here, `Milestone` cannot be constructed without evidence and
cannot be constructed with promise language at all.

The five categories VA-8 requires -- stored facts, retrieved observations, derived analysis,
recommendations, unknowns -- are not re-derived here. They are exactly the existing
`EvidenceKind` values, so `CoachingReport` partitions the claims a turn already produced by
`Claim.weakest_kind`. That matters: a second opinion about which category a statement belongs
in could disagree with the grounding layer, and the disagreement would be invisible.

No model is required anywhere in this file, and none is called from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from app.agent.contracts import AgentTurn, Claim, EvidenceKind, EvidenceRef, GroundedAnswer, Intent

#: The horizons the product builds. Not a range: each one has its own milestone budget below,
#: and a horizon without one would produce a schedule nobody designed.
HORIZONS = (7, 30, 60, 90)

#: How many milestones fit in each horizon. A 7-day plan with eight milestones is a list of
#: everything the student is behind on, which is not a plan and is actively discouraging.
_MILESTONE_BUDGET = {7: 2, 30: 4, 60: 6, 90: 8}

#: Language a milestone may never contain, enforced at construction.
#:
#: These are not stylistic preferences. "You will get a job" is a prediction about a hiring
#: decision made by people this system has never seen, and "improve your score by 40%" is a
#: number with no evidence behind it. A coaching product that says either has mis-sold itself,
#: and the cost lands on a student making real decisions about their time.
_FORBIDDEN_PROMISES = (
    "guarantee",
    "guaranteed",
    "will get you",
    "will land",
    "will be placed",
    "you will get",
    "ensures you",
    "assured",
    "100%",
)

#: Rough effort per milestone kind, in hours. Honest about being rough: these are planning
#: figures used to tell a student when a plan does not fit the time they said they have, not
#: estimates of how long learning a skill takes.
_EFFORT = {"evidence": 6, "practice": 4, "learn": 10, "process": 2}

_MAX_LISTED = 5


class MilestoneKind(str, Enum):
    """What sort of work a milestone is, which decides its order and its effort estimate."""

    EVIDENCE = "evidence"  # prove a skill the student plausibly already has
    PRACTICE = "practice"  # drill a repeatedly-flagged weakness
    LEARN = "learn"  # acquire something the market asked for and they lack
    PROCESS = "process"  # tracking, analysing a goal, generating a path


#: Order within a horizon. Evidence first because it is the cheapest way to move readiness and
#: usually needs no new learning; process last because it unblocks later work rather than being
#: the work. Not alphabetical, and not the order the tools happened to return things in.
_KIND_ORDER = (MilestoneKind.EVIDENCE, MilestoneKind.PRACTICE, MilestoneKind.LEARN, MilestoneKind.PROCESS)


@dataclass(frozen=True)
class Milestone:
    """One ordered, evidence-backed step in a plan."""

    horizon_days: int
    kind: MilestoneKind
    title: str
    #: Why this step, in terms of the student's own situation. Shown to them.
    why: str
    evidence: tuple[EvidenceRef, ...]
    effort_hours: int

    MAX_TEXT = 200

    def __post_init__(self) -> None:
        if self.horizon_days not in HORIZONS:
            raise ValueError(f"milestone horizon must be one of {HORIZONS}")
        if not self.title.strip() or not self.why.strip():
            raise ValueError("a milestone needs a title and a reason")
        if not self.evidence:
            # The same invariant `Claim` holds, for the same reason: a step with no support is
            # a guess about someone's career presented as a plan.
            raise ValueError(f"milestone {self.title[:40]!r} has no evidence")
        if self.effort_hours <= 0:
            raise ValueError("a milestone needs a positive effort estimate")
        lowered = f"{self.title} {self.why}".lower()
        for banned in _FORBIDDEN_PROMISES:
            if banned in lowered:
                raise ValueError(f"milestone promises an outcome it cannot support: {banned!r}")
        object.__setattr__(self, "title", self.title.strip()[: self.MAX_TEXT])
        object.__setattr__(self, "why", self.why.strip()[: self.MAX_TEXT])

    @property
    def weakest_kind(self) -> EvidenceKind:
        order = [
            EvidenceKind.UNKNOWN,
            EvidenceKind.RECOMMENDED,
            EvidenceKind.DERIVED,
            EvidenceKind.LIVE_OBSERVED,
            EvidenceKind.KNOWN,
        ]
        return min((ref.kind for ref in self.evidence), key=order.index)


@dataclass(frozen=True)
class CoachingPlan:
    """An ordered set of milestones for one horizon, with what it could not establish."""

    horizon_days: int
    milestones: tuple[Milestone, ...] = ()
    goal: Optional[str] = None
    weekly_hours: Optional[int] = None
    uncertainties: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.horizon_days not in HORIZONS:
            raise ValueError(f"plan horizon must be one of {HORIZONS}")
        if any(m.horizon_days != self.horizon_days for m in self.milestones):
            raise ValueError("every milestone must belong to the plan's horizon")

    @property
    def total_effort_hours(self) -> int:
        return sum(m.effort_hours for m in self.milestones)

    @property
    def capacity_hours(self) -> Optional[int]:
        """What the student said they have for this horizon, or None if they did not say."""
        if self.weekly_hours is None:
            return None
        return int(self.weekly_hours * (self.horizon_days / 7))

    @property
    def fits_time_budget(self) -> Optional[bool]:
        capacity = self.capacity_hours
        return None if capacity is None else self.total_effort_hours <= capacity

    def to_payload(self) -> dict[str, Any]:
        return {
            "horizon_days": self.horizon_days,
            "goal": self.goal,
            "weekly_hours": self.weekly_hours,
            "total_effort_hours": self.total_effort_hours,
            "capacity_hours": self.capacity_hours,
            "fits_time_budget": self.fits_time_budget,
            "milestones": [
                {
                    "kind": m.kind.value,
                    "title": m.title,
                    "why": m.why,
                    "effort_hours": m.effort_hours,
                    "basis": m.weakest_kind.value,
                    "evidence": [ref.describe() for ref in m.evidence],
                }
                for m in self.milestones
            ],
            "uncertainties": list(self.uncertainties),
        }


# --------------------------------------------------------------------------- builders


def _known(tool: str, summary: str) -> EvidenceRef:
    return EvidenceRef(kind=EvidenceKind.KNOWN, tool=tool, summary=summary)


def _strong_skills(passport: Any) -> set[str]:
    """Skills with at least 60% recorded confidence.

    The same threshold `grounding.skill_gap` uses, and deliberately the same constant's
    meaning: two different thresholds for "the student has this skill" would make the gap the
    plan closes different from the gap the answer reported.
    """
    return {
        (s.skill or "").lower()
        for s in (getattr(passport, "skills", ()) or ())
        if (s.confidence_percent or 0) >= 60
    }


def _candidates(outputs: Mapping[str, Any]) -> list[Milestone]:
    """Every milestone the evidence supports, unordered and unbudgeted.

    Built at horizon 90 and re-stamped by `build_plan`, so one description of what the work is
    serves every horizon and the horizons differ only in how much of it fits.
    """
    found: list[Milestone] = []
    readiness = outputs.get("career_twin.get_readiness")
    passport = outputs.get("skills.get_passport")
    weaknesses = outputs.get("weaknesses.list")
    goals = outputs.get("career_goals.list")
    requirements = outputs.get("world.get_role_requirements")

    def add(kind: MilestoneKind, title: str, why: str, evidence: tuple[EvidenceRef, ...]) -> None:
        found.append(
            Milestone(
                horizon_days=90,
                kind=kind,
                title=title,
                why=why,
                evidence=evidence,
                effort_hours=_EFFORT[kind.value],
            )
        )

    if readiness is not None and getattr(readiness, "biggest_gap_skill", None):
        gap = readiness.biggest_gap_skill
        coverage = getattr(readiness, "evidence_coverage_percent", None)
        is_estimate = bool(getattr(readiness, "is_estimate", False))
        why = f"It is the largest recorded gap for {getattr(readiness, 'display_role_name', None) or 'your target role'}"
        if coverage is not None:
            why += f", and your evidence coverage is {coverage}%"
            if is_estimate:
                # Carried through rather than dropped. A number the twin itself marks as an
                # estimate must not be quoted back as a measurement.
                why += " (an estimate)"
        add(
            MilestoneKind.EVIDENCE,
            f"Produce verifiable evidence for {gap}",
            why + ".",
            (_known("career_twin.get_readiness", "the recorded biggest gap"),),
        )

    for item in list(getattr(weaknesses, "weaknesses", ()) or ())[:3]:
        # `skill_key`, not `skill`: that is what `read_tools.WeaknessItem` exposes. The first
        # version read `skill`, got None for every row and silently built no practice
        # milestones at all -- caught by the database-backed tests, and missed by the unit
        # tests because their fakes used the wrong field name too. Hence no getattr fallback
        # here: the shapes have to match, or the next mismatch hides the same way.
        skill = getattr(item, "skill_key", None)
        if not skill:
            continue
        occurrences = getattr(item, "occurrences", None)
        add(
            MilestoneKind.PRACTICE,
            f"Drill {skill} until it stops recurring",
            f"It has been flagged {occurrences or 'more than once'} time(s) in your own assessments.",
            (_known("weaknesses.list", f"{skill} flagged {occurrences or 'repeatedly'}"),),
        )

    have = _strong_skills(passport)
    for row in list(getattr(requirements, "skills", ()) or ())[:_MAX_LISTED]:
        skill = getattr(row, "skill", None)
        if not skill or skill.lower() in have:
            continue
        hits = getattr(row, "openings_mentioning", None)
        size = getattr(row, "sample_size", None)
        add(
            MilestoneKind.LEARN,
            f"Learn {skill} to a demonstrable level",
            # The sample is in the sentence, so "the market wants this" cannot be read as a
            # claim about the whole market when it came from a handful of adverts.
            f"{hits} of {size} retrieved adverts named it, and you have no strong evidence for it.",
            (
                EvidenceRef(
                    kind=EvidenceKind.DERIVED,
                    tool="world.get_role_requirements",
                    summary=f"{skill} in {hits}/{size} adverts, unmatched against your passport",
                    sample=f"{size} advert(s)",
                ),
            ),
        )

    if readiness is not None and getattr(readiness, "applications_total", None) == 0:
        add(
            MilestoneKind.PROCESS,
            "Start tracking applications in the job tracker",
            "You have no recorded applications, so nothing yet shows how your attempts are going.",
            (_known("career_twin.get_readiness", "applications_total = 0"),),
        )

    unanalysed = [g for g in (getattr(goals, "goals", ()) or ()) if not getattr(g, "is_analyzed", False)]
    if unanalysed:
        add(
            MilestoneKind.PROCESS,
            "Run the analysis on your recorded career goals",
            f"{len(unanalysed)} recorded goal(s) have no requirement breakdown, so they cannot be compared yet.",
            (_known("career_goals.list", f"{len(unanalysed)} unanalysed goal(s)"),),
        )

    return found


def build_plan(
    horizon_days: int,
    outputs: Mapping[str, Any],
    *,
    goal: Optional[str] = None,
    weekly_hours: Optional[int] = None,
) -> CoachingPlan:
    """A bounded, ordered plan for one horizon, from this turn's tool outputs only.

    Deterministic: the same outputs give the same plan. Nothing is invented -- a milestone
    exists because a tool returned the evidence for it, so a student with no recorded data gets
    a plan with no milestones and an explicit reason rather than a plausible-looking schedule.
    """
    if horizon_days not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")

    candidates = _candidates(outputs)
    ordered = sorted(candidates, key=lambda m: _KIND_ORDER.index(m.kind))
    budget = _MILESTONE_BUDGET[horizon_days]
    kept = ordered[:budget]
    milestones = tuple(
        Milestone(
            horizon_days=horizon_days,
            kind=m.kind,
            title=m.title,
            why=m.why,
            evidence=m.evidence,
            effort_hours=m.effort_hours,
        )
        for m in kept
    )

    uncertainties: list[str] = []
    if not milestones:
        uncertainties.append(
            "I do not have enough recorded evidence about you to build a plan yet. "
            "Completing an assessment or setting a target role would give me something to work from."
        )
    dropped = len(ordered) - len(kept)
    if dropped > 0:
        # Said out loud. Silently truncating would present a partial plan as the whole of what
        # the evidence supports.
        uncertainties.append(
            f"{dropped} other thing(s) the evidence supports did not fit in {horizon_days} days; "
            "ask for a longer horizon to see them."
        )
    if "world.get_role_requirements" not in outputs:
        uncertainties.append(
            "This plan uses only your own records -- I could not retrieve what the market "
            "currently asks for, so it may miss requirements that are not in your profile."
        )

    plan = CoachingPlan(
        horizon_days=horizon_days,
        milestones=milestones,
        goal=goal,
        weekly_hours=weekly_hours,
        uncertainties=tuple(uncertainties),
    )
    if plan.fits_time_budget is False:
        # Reported, not resolved by dropping milestones: a plan quietly shortened to fit would
        # read as achievable when it is no longer the plan the evidence supports.
        return CoachingPlan(
            horizon_days=plan.horizon_days,
            milestones=plan.milestones,
            goal=plan.goal,
            weekly_hours=plan.weekly_hours,
            uncertainties=plan.uncertainties
            + (
                f"At {weekly_hours} hour(s) a week this is about {plan.total_effort_hours} hour(s) of work "
                f"against roughly {plan.capacity_hours} available, so it will not all fit.",
            ),
        )
    return plan


def next_step(outputs: Mapping[str, Any]) -> tuple[tuple[Claim, ...], tuple[str, ...]]:
    """The single most useful next thing, for daily coaching.

    A recommendation, and typed as one: `EvidenceKind.RECOMMENDED` so it can never be read back
    as a fact about the student. It is chosen deterministically from what the tools returned,
    in the order a coach would: an unfinished mission today, then the twin's own hint, then the
    top recurring weakness.
    """
    mission = outputs.get("missions.get_today")
    readiness = outputs.get("career_twin.get_readiness")
    weaknesses = outputs.get("weaknesses.list")

    if mission is not None and getattr(mission, "has_mission", False):
        status = getattr(mission, "status", None)
        if status not in ("completed", "complete", "done"):
            return (
                Claim(
                    f"Start with today's mission: {getattr(mission, 'title', None)}.",
                    (
                        EvidenceRef(
                            kind=EvidenceKind.RECOMMENDED,
                            tool="missions.get_today",
                            summary=f"today's mission, status {status}",
                        ),
                    ),
                ),
            ), ()

    hint = getattr(readiness, "next_action_hint", None) if readiness is not None else None
    if hint:
        return (
            Claim(
                f"Suggested next step: {hint}",
                (
                    EvidenceRef(
                        kind=EvidenceKind.RECOMMENDED,
                        tool="career_twin.get_readiness",
                        summary="the twin's own next-action hint",
                    ),
                ),
            ),
        ), ()

    top = next(iter(getattr(weaknesses, "weaknesses", ()) or ()), None)
    if top is not None and getattr(top, "skill_key", None):
        return (
            Claim(
                f"Suggested next step: practise {top.skill_key}, your most frequently flagged weakness.",
                (
                    EvidenceRef(
                        kind=EvidenceKind.RECOMMENDED,
                        tool="weaknesses.list",
                        summary=f"{top.skill_key} flagged {getattr(top, 'occurrences', 'repeatedly')}",
                    ),
                ),
            ),
        ), ()

    return (), (
        "I do not have enough recorded progress to suggest a specific next step yet.",
    )


def role_fit(outputs: Mapping[str, Any]) -> tuple[tuple[Claim, ...], tuple[str, ...]]:
    """A derived comparison of recorded goals against recorded readiness.

    `DERIVED`, never `KNOWN`: it is a comparison this code performed, not a fact either tool
    reported. And it compares only goals the student recorded -- suggesting a role they never
    mentioned would be the system choosing their career for them on no evidence.
    """
    goals = list(getattr(outputs.get("career_goals.list"), "goals", ()) or ())
    readiness = outputs.get("career_twin.get_readiness")
    claims: list[Claim] = []
    unknowns: list[str] = []

    if not goals:
        return (), (
            "You have not recorded any career goals, so there is nothing for me to compare. "
            "Adding one or two target roles would let me weigh them against your evidence.",
        )

    coverage = getattr(readiness, "evidence_coverage_percent", None) if readiness is not None else None
    twin_role = getattr(readiness, "display_role_name", None) if readiness is not None else None

    analysed = [g for g in goals if getattr(g, "is_analyzed", False)]
    if not analysed:
        unknowns.append(
            "None of your recorded goals has been analysed, so I can list them but cannot rank "
            "them against your skills."
        )
    if coverage is None:
        unknowns.append("Your readiness has not been computed yet, so I have no baseline to compare against.")

    if twin_role and coverage is not None:
        aligned = [g for g in goals if twin_role.lower() in (g.title or "").lower()]
        if aligned:
            claims.append(
                Claim(
                    f"{len(aligned)} of your recorded goals line up with {twin_role}, "
                    f"where your evidence coverage is {coverage}%.",
                    (
                        _known("career_goals.list", f"{len(goals)} recorded goal(s)"),
                        EvidenceRef(
                            kind=EvidenceKind.DERIVED,
                            tool="career_twin.get_readiness",
                            summary=f"{len(aligned)} goal title(s) matched against the twin's role",
                        ),
                    ),
                )
            )
        else:
            # Stated as a mismatch, not resolved. Which one is right is the student's call, and
            # guessing would quietly override either their goals or their target role.
            unknowns.append(
                f"None of your recorded goals mentions {twin_role}, which is the role your readiness "
                "is measured against -- so one of the two is out of date."
            )

    return tuple(claims), tuple(unknowns)


# --------------------------------------------------------------------------- the report


@dataclass(frozen=True)
class CoachingReport:
    """The structured coaching result: the five evidence categories, kept apart.

    Partitioned from a turn's claims by `Claim.weakest_kind` rather than re-decided here, so
    this view cannot disagree with the grounding layer about what is a fact and what is a
    suggestion.
    """

    intent: Intent
    stored_facts: tuple[Claim, ...] = ()
    retrieved_observations: tuple[Claim, ...] = ()
    derived_analysis: tuple[Claim, ...] = ()
    recommendations: tuple[Claim, ...] = ()
    unknowns: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    tools_used: tuple[str, ...] = ()
    clarifying_question: Optional[str] = None
    plan: Optional[CoachingPlan] = None
    freshness_notes: tuple[str, ...] = field(default=())

    @classmethod
    def from_answer(
        cls, answer: GroundedAnswer, *, plan: Optional[CoachingPlan] = None
    ) -> "CoachingReport":
        buckets: dict[EvidenceKind, list[Claim]] = {kind: [] for kind in EvidenceKind}
        for claim in answer.claims:
            buckets[claim.weakest_kind].append(claim)
        return cls(
            intent=answer.intent,
            stored_facts=tuple(buckets[EvidenceKind.KNOWN]),
            retrieved_observations=tuple(buckets[EvidenceKind.LIVE_OBSERVED]),
            derived_analysis=tuple(buckets[EvidenceKind.DERIVED]),
            recommendations=tuple(buckets[EvidenceKind.RECOMMENDED]),
            # An UNKNOWN-kind claim is a statement whose own support says "we looked and cannot
            # say", so it belongs with the uncertainties rather than in a category of fact.
            unknowns=tuple(c.text for c in buckets[EvidenceKind.UNKNOWN]) + answer.uncertainties,
            failures=answer.failures,
            tools_used=answer.tools_used,
            clarifying_question=answer.clarifying_question,
            plan=plan,
            freshness_notes=_freshness_notes(answer.claims),
        )

    @property
    def has_content(self) -> bool:
        return bool(
            self.stored_facts
            or self.retrieved_observations
            or self.derived_analysis
            or self.recommendations
            or self.unknowns
            or self.clarifying_question
        )

    def to_payload(self) -> dict[str, Any]:
        def render(claims: Iterable[Claim]) -> list[dict[str, Any]]:
            return [
                {"text": c.text, "basis": c.weakest_kind.value, "evidence": [r.describe() for r in c.evidence]}
                for c in claims
            ]

        return {
            "intent": self.intent.value,
            "stored_facts": render(self.stored_facts),
            "retrieved_observations": render(self.retrieved_observations),
            "derived_analysis": render(self.derived_analysis),
            "recommendations": render(self.recommendations),
            "unknowns": list(self.unknowns),
            "failures": list(self.failures),
            "tools_used": list(self.tools_used),
            "clarifying_question": self.clarifying_question,
            "freshness": list(self.freshness_notes),
            "plan": self.plan.to_payload() if self.plan else None,
        }


@dataclass(frozen=True)
class CoachingTurn:
    """A turn and its coaching view, together.

    Both, rather than the report alone, because the audit trail -- which tools ran, how long it
    took, whether a model was consulted -- belongs with the answer and is what makes a coaching
    response checkable after the fact.
    """

    turn: AgentTurn
    report: CoachingReport

    def to_payload(self) -> dict[str, Any]:
        payload = self.turn.to_payload()
        payload["coaching"] = self.report.to_payload()
        return payload


def _freshness_notes(claims: tuple[Claim, ...]) -> tuple[str, ...]:
    """One line per retrieved source, naming provider, freshness and retrieval time.

    Only for evidence that actually has a provider. A personal database row has no freshness
    policy, and attaching one would imply a retrieval that never happened.
    """
    notes: list[str] = []
    for claim in claims:
        for ref in claim.evidence:
            if not ref.provider:
                continue
            when = ref.retrieved_at.isoformat() if ref.retrieved_at else "an unrecorded time"
            notes.append(f"{ref.tool}: {ref.provider}, {ref.freshness or 'freshness unknown'}, retrieved {when}")
    return tuple(dict.fromkeys(notes))
