"""Gap classification -- turning facts into the eight kinds of thing a student can act on.

A low number is not a gap. "Low SQL score" does not tell anyone what to do; "you have studied
SQL and still test poorly on it" says *practise*, while "your SQL is verified but your resume
never mentions it" says *rewrite a bullet point*. The category is the actionable part, and
picking it wrong sends a student to the wrong screen for weeks.

The eight categories are the product's own taxonomy. Within SKILL_GAP, `sub_kind` preserves the
distinction that actually decides the destination:

    SKILL_GAP / knowledge   nothing here at all               -> Roadmap
    SKILL_GAP / ability     studied or claimed, tests poorly  -> Practice
    EVIDENCE_GAP            can perhaps do it, cannot prove it -> Project Builder
    RESUME_GAP              proved it, resume omits it        -> Resume / ATS
    EXPERIENCE_GAP          seniority misfit on real adverts  -> Job Hunt / Career Path
    INTERVIEW_GAP           delivery, not knowledge           -> Interview
    APPLICATION_GAP         the funnel itself has stalled     -> Tracker
    JOB_TARGETING_GAP       aiming at jobs the profile misses -> Job Hunt / Career Path
    PROFILE_GAP             we cannot see enough to judge     -> Profile / Resume / Interview

Order matters inside the skill loop and is not arbitrary. A direct measurement is checked first
because precedence.py ranks `observed` above the `inferred` rollup and above any claim -- a
student who scored 41% on an SQL assessment has an ability problem regardless of how many
places also mention SQL. Checking breadth first would have quietly reclassified that as an
evidence problem and sent them to Project Builder instead of practice.

**The known-requirement rule.** A gap is emitted when we know something is required *and*
observe it is not there. It is never emitted merely because a source is absent:

  * no target role         -> no skill gaps at all (nothing defines what is required)
  * no resume analysed     -> no RESUME_GAP (we did not look at the resume)
  * no interviews          -> no INTERVIEW_GAP (we never watched them interview)
  * no tracked jobs        -> no EXPERIENCE_GAP or JOB_TARGETING_GAP (no adverts to misfit)
  * too few applications   -> no APPLICATION_GAP (a ratio over two attempts is noise)
  * role skill, no evidence -> a real SKILL_GAP (the role requires it; we looked; nothing)

The last line is the one worth being careful about: it is not "missing data became a gap". The
requirement is known, the search was performed, and the result was nothing. That is an
observation, and it is the same one next_best_action_service already treats as its top tier.

Interview and recruiter-attack dimensions never enter the skill loop. They are namespaced
(`interview:clarity`) and filtered through signals.is_interview_subject, because a role weight
and a "go learn clarity" recommendation are both nonsense.

Nothing here recomputes a score. Experience fit and role alignment come from
`JobListingMatch.component_scores`, which job_hunt/matching produced -- this module reads that
verdict, it does not form its own.

Pure and deterministic: takes a SignalBundle, returns a sorted tuple, reads no clock and
touches no database.
"""

from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict

from app.services.career_strategy import signals as signals_module
from app.services.career_strategy.signals import SignalBundle, StrategySignal
from app.services.skill_matching import skill_present
from app.services.weakness_service import INTERVIEW_DIMENSION_THRESHOLD

GAP_CATEGORIES: Tuple[str, ...] = (
    "SKILL_GAP",
    "EVIDENCE_GAP",
    "EXPERIENCE_GAP",
    "RESUME_GAP",
    "INTERVIEW_GAP",
    "APPLICATION_GAP",
    "JOB_TARGETING_GAP",
    "PROFILE_GAP",
)

# Only meaningful on SKILL_GAP, where it decides between Roadmap and Practice. The product
# taxonomy has one skill category; the routing question inside it is still two questions, and
# collapsing them would send a student who has studied a topic back to study it again.
SKILL_SUB_KINDS: Tuple[str, ...] = ("knowledge", "ability")

# Destination *keys*, not routes. Phase 4 maps each to a real Android route and a test there
# asserts every key resolves -- naming a route string here would put navigation knowledge in
# the backend and let a renamed screen rot silently into a dead link.
DESTINATIONS: Tuple[str, ...] = (
    "roadmap",
    "practice",
    "project_builder",
    "skill_passport",
    "resume_ats",
    "interview",
    "job_hunt",
    "tracker",
    "career_path",
    "profile",
)

_CATEGORY_DESTINATION = {
    "SKILL_GAP": "roadmap",
    "EVIDENCE_GAP": "project_builder",
    "EXPERIENCE_GAP": "career_path",
    "RESUME_GAP": "resume_ats",
    "INTERVIEW_GAP": "interview",
    "APPLICATION_GAP": "tracker",
    "JOB_TARGETING_GAP": "job_hunt",
    "PROFILE_GAP": "profile",
}

SUBJECT_KINDS: Tuple[str, ...] = (
    "skill",
    "interview_dimension",
    "match_component",
    "funnel",
    "profile_field",
)

# The product already draws its "weak" line at 0.6 (weakness_service.INTERVIEW_DIMENSION_THRESHOLD,
# shared with weakness_record_service for exactly this reason). Reused rather than redeclared:
# two thresholds would let a skill be weak enough for Interview Memory to track and not weak
# enough for Strategy to mention, which is indefensible to a student looking at both screens.
WEAK_SCORE_THRESHOLD = INTERVIEW_DIMENSION_THRESHOLD

# Below this a weak result is severe rather than merely weak -- weakness_service's own
# high-severity line, again reused rather than reinvented.
SEVERE_SCORE_THRESHOLD = 0.4

# A role skill at or above this weight is core to the role (next_best_action_service's
# _CORE_WEIGHT_THRESHOLD uses the same figure for its critical-gap tier).
CORE_WEIGHT_THRESHOLD = 1.0

# A skill missing from at least this share of the student's scored tracked jobs is a severe
# alignment problem rather than an incidental one.
HIGH_DEMAND_SHARE = 0.5

# Average match component below which the student's tracked adverts are, on the whole, a poor
# fit on that dimension. Deliberately the same 0.6 line the rest of the product calls "weak".
WEAK_COMPONENT_THRESHOLD = 0.6

# A tracked job has to score at least this well before "you are ready, start applying" is honest
# advice about it. Shares contradictions.STRONG_MATCH_THRESHOLD's value deliberately: one advert
# must not read as a strong match in one part of the strategy and a weak one in another.
SUITABLE_MATCH_THRESHOLD = 0.7

# A progression rate at or below this, over enough applications to be readable, is a funnel
# that is not converting.
WEAK_PROGRESSION_RATE = 0.2

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

# The measured evidence sources, in the order a tie is broken. Assessment first because it is
# the most deliberate measurement the product takes.
_MEASURED_SOURCES = ("assessment", "practice", "interview", "recruiter_attack")


class StrategyGap(BaseModel):
    """One classified, actionable gap, carrying everything the bottleneck engine needs to rank it.

    `signal_keys` is not decoration: it is the audit trail that lets a "why this?" explanation
    cite real sources instead of generating plausible-sounding ones, and it is how a bug in
    classification is traced back to the fact that caused it.
    """

    model_config = ConfigDict(frozen=True)

    category: str
    subject: str
    # Guards the interview-dimension mistake structurally rather than by convention: anything
    # that is not `skill` must never be given a role weight or matched against a skill name.
    subject_kind: str
    severity: str
    # Only set on SKILL_GAP. None everywhere else.
    sub_kind: Optional[str] = None
    # None for non-skill subjects, and for skills the role does not weight.
    role_weight: Optional[float] = None
    signal_keys: List[str] = []
    destination: str = "profile"
    reason: str = ""
    is_actionable: bool = True
    # Share of the student's own scored tracked jobs that name this skill as missing. Never a
    # market statistic -- only their shortlist. None when they track no scored jobs.
    job_demand_share: Optional[float] = None


def _severity(*, base: str, escalate: bool = False, deescalate: bool = False) -> str:
    if escalate:
        return "high"
    if deescalate and base != "high":
        return "low"
    return base


def _measured_signal(bundle: SignalBundle, skill: str) -> Optional[StrategySignal]:
    """The weakest measured score for a skill, or None if nothing measured it.

    Deliberately not an average across sources: averaging a 90% assessment with a 40% practice
    run invents a 65% nobody scored. The lowest measured score is taken instead, because the
    question this feeds is "is there a demonstrated weakness", and one genuinely poor
    measurement is a demonstrated weakness even if another source went well.
    """
    candidates = [
        signal
        for signal in bundle.for_subject(skill)
        if signal.source in _MEASURED_SOURCES and signal.value is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda signal: (signal.value, _MEASURED_SOURCES.index(signal.source)))


def _resume_mentions(bundle: SignalBundle, skill: str) -> Optional[bool]:
    """Tri-state. None means no resume analysis exists, so the question was never asked -- which
    is different from asking and finding the skill absent."""
    if bundle.resume_skills is None:
        return None
    return skill_present(skill, list(bundle.resume_skills))


def _demand_share(bundle: SignalBundle, skill: str) -> Optional[float]:
    signal = bundle.by_key(f"job_demand:{skill}")
    return signal.value if signal is not None else None


def _classify_skill(bundle: SignalBundle, skill: str, weight: Optional[float]) -> Optional[StrategyGap]:
    view = bundle.evidence_by_skill.get(skill)
    if view is None:  # pragma: no cover -- collect() always populates every skill it lists
        return None

    evidence_key = f"evidence:{skill}"
    measured = _measured_signal(bundle, skill)
    demand = _demand_share(bundle, skill)
    is_core = weight is not None and weight >= CORE_WEIGHT_THRESHOLD
    high_demand = demand is not None and demand >= HIGH_DEMAND_SHARE
    minor = weight is not None and weight < 0.5

    def build(
        category: str, severity: str, reason: str, keys: List[str], *, sub_kind: Optional[str] = None
    ) -> StrategyGap:
        destination = _CATEGORY_DESTINATION[category]
        if category == "SKILL_GAP" and sub_kind == "ability":
            # Studied it and still tests poorly: more reading is the wrong answer.
            destination = "practice"
        return StrategyGap(
            category=category,
            subject=skill,
            subject_kind="skill",
            severity=severity,
            sub_kind=sub_kind,
            role_weight=weight,
            signal_keys=sorted(set(keys)),
            destination=destination,
            reason=reason,
            job_demand_share=demand,
        )

    # 1 -- a direct measurement showing underperformance. Checked first: `observed` outranks
    # both the `inferred` evidence rollup and any claim (precedence.py), so no amount of
    # breadth reclassifies a poor score into something else.
    if measured is not None and measured.value < WEAK_SCORE_THRESHOLD:
        severe = measured.value < SEVERE_SCORE_THRESHOLD
        return build(
            "SKILL_GAP",
            _severity(base="medium", escalate=severe or is_core or high_demand, deescalate=minor),
            f"{skill}: measured {measured.value:.0%} ({measured.source}), below the {WEAK_SCORE_THRESHOLD:.0%} mark.",
            [measured.key, evidence_key],
            sub_kind="ability",
        )

    # 2 -- real, independent evidence that the resume fails to carry. This is the distinction
    # the whole feature turns on: the student can do it, so telling them to learn it would be
    # wrong; the recruiter's filter simply never sees it.
    mentioned = _resume_mentions(bundle, skill)
    if view.state in ("strong", "verified") and mentioned is False:
        return build(
            "RESUME_GAP",
            _severity(base="medium", escalate=is_core or high_demand, deescalate=minor),
            f"{skill} is {view.state} in your evidence but does not appear on your analysed resume.",
            [evidence_key, "resume:on_file"],
        )

    # 3 -- evidence exists but every dated piece of it has aged past evidence_engine's staleness
    # window. Proof, not knowledge, is what expired, so this is an evidence gap.
    if view.state == "stale":
        return build(
            "EVIDENCE_GAP",
            _severity(base="medium", escalate=is_core and high_demand, deescalate=minor),
            f"Your {skill} evidence has not been demonstrated recently.",
            [evidence_key],
        )

    # 4 -- nothing at all. evidence_engine's `claimed` state means the item list was empty, so
    # this is "the role needs it, we looked, there is nothing", not "we did not look".
    if view.state == "claimed":
        return build(
            "SKILL_GAP",
            _severity(base="medium", escalate=is_core or high_demand, deescalate=minor),
            f"{skill} has no evidence of any kind yet.",
            [evidence_key],
            sub_kind="knowledge",
        )

    # 5 -- everything we hold is the student's own assertion. They may well be able to do it;
    # nothing independent shows it, which is a proof problem rather than a learning one.
    if view.items and all(item.self_asserted for item in view.items):
        claim_keys = [f"{item.source}:{skill}" for item in view.items]
        return build(
            "EVIDENCE_GAP",
            _severity(base="medium", escalate=is_core or high_demand, deescalate=minor),
            f"{skill} appears only in what you have told us; nothing independent backs it yet.",
            [evidence_key, *claim_keys],
        )

    # 6 -- measured acceptably, or independently evidenced and on the resume. Not a gap. A
    # strength is the bottleneck engine's business: strengths exist to suppress recommendations.
    return None


def _interview_gaps(bundle: SignalBundle) -> List[StrategyGap]:
    """Weak interview/recruiter dimensions, preferring Interview Memory's cross-session status
    over a single evaluation where a record exists.

    A record only exists once a dimension has actually been weak at least once, and its status
    machine is the thing that can tell "weak in five straight interviews" from "weak once" --
    so where it exists it is strictly more informative than the latest score, and a CLOSED
    record correctly suppresses a gap that a single old evaluation would still raise.
    """
    gaps: List[StrategyGap] = []
    seen: set = set()

    # Read the WeaknessRecord rows themselves, not the display label built from them.
    # `status` and `occurrences` decide whether a gap is emitted at all and how severe it is,
    # which makes them data; recovering them by splitting a formatted string would mean a
    # copy-edit to that string could silently flip a severity or suppress a gap entirely.
    for record in sorted(bundle.weakness_records, key=lambda r: r.skill_key):
        seen.add(record.skill_key)
        if record.status == "CLOSED":
            continue
        label = record.skill_key.split(":")[-1].replace("_", " ")
        gaps.append(
            StrategyGap(
                category="INTERVIEW_GAP",
                subject=record.skill_key,
                subject_kind="interview_dimension",
                severity="high" if record.occurrences >= 2 else "medium",
                role_weight=None,
                signal_keys=[f"weakness_memory:{record.skill_key}"],
                destination=_CATEGORY_DESTINATION["INTERVIEW_GAP"],
                reason=(
                    f"{label} has been flagged {record.occurrences} time"
                    f"{'s' if record.occurrences != 1 else ''} across your interviews ({record.status})."
                ),
            )
        )

    for signal in bundle.signals:
        if not signals_module.is_interview_subject(signal.subject):
            continue
        if signal.key.startswith("weakness_memory:") or signal.subject in seen:
            continue
        if signal.value is None or signal.value >= WEAK_SCORE_THRESHOLD:
            continue
        gaps.append(
            StrategyGap(
                category="INTERVIEW_GAP",
                subject=signal.subject or "",
                subject_kind="interview_dimension",
                severity="high" if signal.value < SEVERE_SCORE_THRESHOLD else "medium",
                role_weight=None,
                signal_keys=[signal.key],
                destination=_CATEGORY_DESTINATION["INTERVIEW_GAP"],
                reason=f"{signal.raw_label} — below the {WEAK_SCORE_THRESHOLD:.0%} mark.",
            )
        )
    return gaps


def _experience_gaps(bundle: SignalBundle) -> List[StrategyGap]:
    """Seniority misfit, taken from the match engine's own verdict.

    `component_scores["experience_fit"]` is what job_hunt/matching already decided about this
    student against these adverts, using its own fresher/junior/mid/senior/lead ladder. Reading
    that average is the entire implementation. Deriving a years-of-experience figure here and
    comparing it to something would be a second experience model disagreeing with the first --
    and would have to invent answers for overlapping roles, internships and career gaps that no
    data in this product supports.
    """
    signal = bundle.by_key("match_component:experience_fit")
    if signal is None or signal.value is None or signal.value >= WEAK_COMPONENT_THRESHOLD:
        return []
    return [
        StrategyGap(
            category="EXPERIENCE_GAP",
            subject="experience_fit",
            subject_kind="match_component",
            severity="high" if signal.value < SEVERE_SCORE_THRESHOLD else "medium",
            role_weight=None,
            signal_keys=[signal.key],
            destination=_CATEGORY_DESTINATION["EXPERIENCE_GAP"],
            reason=(
                f"The jobs you are tracking sit above your recorded experience level "
                f"({signal.value:.0%} average fit)."
            ),
        )
    ]


def _nothing_else_is_in_the_way(bundle: SignalBundle, prior_gaps: Sequence[StrategyGap]) -> bool:
    """Is preparation itself the problem, or is applying the problem?

    Answered from gaps this same pass already found -- not from a readiness number computed
    here. That matters: a second readiness calculation living in the gap classifier would be a
    second source of truth for the one figure the whole product is built around, and it would
    be free to disagree with Career Twin on the same screen.

    EXPERIENCE_GAP is excluded deliberately. It is real and it is reported, but it is never
    actionable -- a fresher cannot acquire three years this month -- so letting it veto "you
    are ready, start applying" would silence the advice for exactly the students who most need
    to hear it.

    An unanswered application-readiness input (no resume on file, no interview ever recorded)
    also blocks. Not because it counts against the student, but because we genuinely cannot
    tell whether they are prepared, and "you appear prepared" is a claim that needs evidence
    behind it rather than an absence of contrary evidence.

    Finally, at least one tracked job has to actually suit them. See below.
    """
    for gap in prior_gaps:
        if gap.severity == "high" and gap.category != "EXPERIENCE_GAP":
            return False
    for signal in bundle.by_category("application_readiness"):
        if signal.evidence_kind == "missing":
            return False
    # And the jobs they are sitting on have to be worth applying to. A queue of adverts the
    # match engine rates poorly is not a conversion problem -- telling that student their
    # bottleneck is "pressing send" would push them at roles they do not fit, which is worse
    # advice than saying nothing. The threshold is matching's own, read from stored scores.
    suitable = [
        signal
        for signal in bundle.signals
        if signal.key.startswith("job_match:")
        and signal.value is not None
        and signal.value >= SUITABLE_MATCH_THRESHOLD
    ]
    return bool(suitable)


def _application_gaps(bundle: SignalBundle, prior_gaps: Sequence[StrategyGap]) -> List[StrategyGap]:
    """The funnel itself. Two distinct problems, never both at once.

    Neither claims causation. "Applications are not progressing" is a fact about the tracker;
    what is causing it is the bottleneck engine's question, and even there it is phrased as
    something the evidence *may* indicate rather than something proved.

    The severity of "saved jobs but never applied" depends on whether anything else is actually
    in the way, because the same observation means two opposite things. For a student with real
    gaps still open it is one item among several. For a student whose preparation is done it is
    *the* thing keeping them from a job, and ranking it mid-table would leave them polishing a
    profile that was already finished. Note what this is not: no probability, no claim about
    whether they would be hired -- only that nothing we can measure is still blocking them.
    """
    tracked = bundle.by_key("tracker:tracked")
    applied = bundle.by_key("tracker:applied")
    rate = bundle.by_key("tracker:progression_rate")

    if tracked is None or tracked.evidence_kind == "missing":
        return []

    # Saving jobs but never applying. The student has done the research and stopped short.
    if applied is not None and applied.evidence_kind == "missing":
        prepared = _nothing_else_is_in_the_way(bundle, prior_gaps)
        reason = (
            f"{tracked.raw_label}, but no applications submitted yet. Nothing else we can "
            "measure is still in your way — the gap now is turning that preparation into "
            "applications."
            if prepared
            else f"{tracked.raw_label}, but no applications submitted yet."
        )
        return [
            StrategyGap(
                category="APPLICATION_GAP",
                subject="applications",
                subject_kind="funnel",
                severity="high" if prepared else "medium",
                role_weight=None,
                signal_keys=["tracker:tracked", "tracker:applied"],
                destination=_CATEGORY_DESTINATION["APPLICATION_GAP"],
                reason=reason,
            )
        ]

    # Applying steadily without conversion. Only once there are enough applications for the
    # ratio to be readable -- signals reports the rate as unknown below that floor.
    if rate is not None and rate.value is not None and rate.value <= WEAK_PROGRESSION_RATE:
        return [
            StrategyGap(
                category="APPLICATION_GAP",
                subject="progression",
                subject_kind="funnel",
                severity="high" if rate.value == 0.0 else "medium",
                role_weight=None,
                signal_keys=["tracker:progression_rate"],
                destination=_CATEGORY_DESTINATION["APPLICATION_GAP"],
                reason=f"{rate.raw_label}. Your application volume is healthy; progression is not following it.",
            )
        ]
    return []


def _job_targeting_gaps(bundle: SignalBundle, role_skills: Sequence[str]) -> List[StrategyGap]:
    """Aiming at jobs the profile does not fit -- two independent readings, both from real data.

    The first is per-skill: something the student's own tracked jobs ask for that their target
    role does not weight. Scoped that way on purpose -- a skill the role *does* weight is
    already classified by the skill loop, and emitting it twice would double-count it in
    ranking. What is left is the genuinely distinct case: the profile is aligned to the chosen
    role but not to the jobs actually being pursued, which is a *targeting* problem and belongs
    in Job Hunt or Career Path rather than in a practice queue.

    The second is the match engine's own `role_alignment` average across those same jobs.

    Only real JobListingMatch rows feed either. No market statistic, no model assumption about
    what employers want.
    """
    gaps: List[StrategyGap] = []

    alignment = bundle.by_key("match_component:role_alignment")
    if alignment is not None and alignment.value is not None and alignment.value < WEAK_COMPONENT_THRESHOLD:
        gaps.append(
            StrategyGap(
                category="JOB_TARGETING_GAP",
                subject="role_alignment",
                subject_kind="match_component",
                severity="high" if alignment.value < SEVERE_SCORE_THRESHOLD else "medium",
                role_weight=None,
                signal_keys=[alignment.key],
                destination=_CATEGORY_DESTINATION["JOB_TARGETING_GAP"],
                reason=(
                    f"The jobs you are tracking align only {alignment.value:.0%} with your profile on average."
                ),
            )
        )

    for signal in bundle.signals:
        if not signal.key.startswith("job_demand:"):
            continue
        skill = signal.subject or ""
        if any(skill_present(role_skill, [skill]) for role_skill in role_skills):
            continue
        share = signal.value
        gaps.append(
            StrategyGap(
                category="JOB_TARGETING_GAP",
                subject=skill,
                subject_kind="skill",
                severity="high" if share is not None and share >= HIGH_DEMAND_SHARE else "medium",
                role_weight=None,
                signal_keys=[signal.key],
                destination=_CATEGORY_DESTINATION["JOB_TARGETING_GAP"],
                reason=f"{skill}: {signal.raw_label.lower()}, and it is not weighted for your target role.",
                job_demand_share=share,
            )
        )
    return gaps


def _profile_gaps(bundle: SignalBundle) -> List[StrategyGap]:
    """What we cannot see. These are not failures -- they are the reason parts of the strategy
    are still blank, and each one names the single thing that would fill it in.

    Deliberately owns resume-on-file and interview-history as well as the plain profile fields.
    Both are inputs job_hunt/readiness.py would report as unknown for *every* job until they
    exist, and the student's action in each case is the same shape: give the product something
    to look at. Splitting them across two categories would report one absence under two names.
    """
    gaps: List[StrategyGap] = []

    role_signal = bundle.by_key("career:target_role")
    if role_signal is not None and role_signal.evidence_kind == "missing":
        gaps.append(
            StrategyGap(
                category="PROFILE_GAP",
                subject="target_role",
                subject_kind="profile_field",
                severity="high",
                role_weight=None,
                signal_keys=["career:target_role"],
                destination="career_path",
                reason="No target role is set, so nothing can be measured against a requirement yet.",
            )
        )

    for key, subject, destination in (
        ("readiness_input:resume", "resume", "resume_ats"),
        ("readiness_input:interview", "interview", "interview"),
    ):
        signal = bundle.by_key(key)
        if signal is None or signal.evidence_kind != "missing":
            continue
        gaps.append(
            StrategyGap(
                category="PROFILE_GAP",
                subject=subject,
                subject_kind="profile_field",
                severity="medium",
                role_weight=None,
                signal_keys=[key],
                destination=destination,
                reason=signal.raw_label,
            )
        )

    for signal in bundle.by_category("identity"):
        if signal.evidence_kind != "missing":
            continue
        gaps.append(
            StrategyGap(
                category="PROFILE_GAP",
                subject=signal.subject or signal.key.split(":")[-1],
                subject_kind="profile_field",
                severity="low",
                role_weight=None,
                signal_keys=[signal.key],
                destination="profile",
                reason=signal.raw_label,
            )
        )
    return gaps


def _sort_key(gap: StrategyGap) -> tuple:
    """Deterministic ordering: severity, then role weight, then category, then subject.

    This is presentation order, not priority -- impact x effort ranking belongs to the
    bottleneck engine and needs inputs this module does not compute. Sorting here only
    guarantees that two identical inputs produce an identical list, which determinism requires
    and which dict/query iteration order would otherwise quietly break.
    """
    return (
        -_SEVERITY_RANK[gap.severity],
        -(gap.role_weight or 0.0),
        gap.category,
        gap.subject,
    )


def classify(bundle: SignalBundle) -> Tuple[StrategyGap, ...]:
    """Every gap the bundle supports, sorted deterministically.

    Returns only profile gaps for a student with no target role -- there is no requirement to
    measure against, and inventing one would be the fabrication this layer exists to prevent.
    """
    gaps: List[StrategyGap] = []

    weights: Dict[str, float] = dict(bundle.role.weights) if bundle.role is not None else {}
    role_skills = [skill for skill in bundle.skills if skill in weights]

    for skill in bundle.skills:
        if signals_module.is_interview_subject(skill):  # pragma: no cover -- filtered upstream
            continue
        if skill not in weights:
            # A resume skill the role does not weight. It is in the universe so contradictions
            # can see it ("strong capability, weak role alignment"), but it is not something the
            # student is failing to meet -- there is no requirement behind it.
            continue
        gap = _classify_skill(bundle, skill, weights.get(skill))
        if gap is not None:
            gaps.append(gap)

    gaps += _interview_gaps(bundle)
    gaps += _experience_gaps(bundle)
    # Takes the gaps found so far: whether "not applying" is the top problem depends on
    # whether anything else is still open. See _nothing_else_is_in_the_way.
    gaps += _application_gaps(bundle, gaps)
    gaps += _job_targeting_gaps(bundle, role_skills)
    gaps += _profile_gaps(bundle)

    return tuple(sorted(gaps, key=_sort_key))
