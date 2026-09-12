"""Impact x effort -- why one gap outranks another, in terms a student can be shown.

Ranking has to be explainable or it is not trustworthy. A screen that says "this is your
biggest problem" and cannot say why is indistinguishable from a screen that guessed. So the
priority of a gap is a product of seven named factors, each derived from a real signal, each
carrying its own one-line note when it materially moved the result. `PriorityFactors` is that
working, kept alongside the number rather than thrown away.

**The raw score is internal.** `priority_score` exists to order a list deterministically; it is
not a measurement of anything, and rendering it as "your interview gap scores 0.42" would claim
a precision this has no basis for. Clients get `impact_band` and `effort_band`, which is what
the product actually promises: high impact, low effort.

**Effort is a category-level property, not a per-student measurement.** Nothing in this product
tracks how long anything takes anyone, so a per-gap estimate would be invented. What is
defensible is the shape of the work: sitting one mock interview is a smaller commitment than
learning a new technology, for everyone, and that ordering is what the bands encode.

**Actionability is a multiplier, not a filter.** An EXPERIENCE_GAP is real and worth reporting
-- the jobs a student tracks genuinely do sit above their recorded level -- but nobody can
manufacture two years of experience this month. Down-weighting it stops the engine from
answering "what should I do next?" with advice that cannot be acted on, without pretending the
gap does not exist.

Pure and deterministic. No clock, no database, no AI.
"""

from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

from app.services.career_strategy.diminishing_returns import PerformanceSeries
from app.services.career_strategy.gaps import CORE_WEIGHT_THRESHOLD, StrategyGap

IMPACT_BANDS: Tuple[str, ...] = ("low", "medium", "high", "very_high")
EFFORT_BANDS: Tuple[str, ...] = ("low", "medium", "high")

# How much of a commitment each category's work actually is. Interview and resume work is a
# single sitting; a new technology or a portfolio project is weeks. Experience is the outlier:
# the work is real but it is measured in jobs, not evenings.
_EFFORT_BY_CATEGORY: Dict[str, str] = {
    "SKILL_GAP": "high",  # overridden to medium for the `ability` sub-kind below
    "EVIDENCE_GAP": "high",
    "EXPERIENCE_GAP": "high",
    "RESUME_GAP": "low",
    "INTERVIEW_GAP": "low",
    "APPLICATION_GAP": "low",
    "JOB_TARGETING_GAP": "low",
    "PROFILE_GAP": "low",
}

# How much of this can actually be acted on in the near term. Everything else is 1.0.
# EXPERIENCE_GAP is the only genuine outlier -- you cannot practise your way to seniority --
# so it is reported but never allowed to become the thing a student is told to go do.
_ACTIONABILITY: Dict[str, float] = {"EXPERIENCE_GAP": 0.25}

_SEVERITY_FACTOR = {"low": 0.4, "medium": 0.7, "high": 1.0}

# How far a category can move a career outcome at all. Without this, every non-skill gap
# inherits a full evidence deficit (there is no evidence confidence to subtract for a profile
# field or a funnel) and an unfilled "branch" field ranks alongside a missing core skill.
# Skills, proof and interview performance are what a hiring decision turns on; a blank profile
# field is worth prompting for and is not in the same class.
_CATEGORY_WEIGHT = {
    "SKILL_GAP": 1.0,
    "EVIDENCE_GAP": 1.0,
    "INTERVIEW_GAP": 1.0,
    "RESUME_GAP": 0.9,
    "JOB_TARGETING_GAP": 0.8,
    "APPLICATION_GAP": 0.8,
    "EXPERIENCE_GAP": 0.7,
    "PROFILE_GAP": 0.4,
}
_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}

# A gap on a skill whose measured history is already strong and stable is worth much less than
# its severity suggests -- this is the diminishing-returns rule expressed as a multiplier.
_STABLE_HIGH_FACTOR = 0.2
# A flat low series means the current approach is not working. The gap is real, but repeating
# the same activity is not the answer, so it is damped rather than promoted.
_STABLE_LOW_FACTOR = 0.6
# A series that is genuinely climbing needs no intervention: leave them to it.
_IMPROVING_FACTOR = 0.5

_IMPACT_THRESHOLDS = ((0.55, "very_high"), (0.35, "high"), (0.18, "medium"))


class PriorityFactors(BaseModel):
    """The working behind a priority, kept so it can be shown rather than asserted."""

    model_config = ConfigDict(frozen=True)

    role_relevance: float
    category_weight: float
    severity: float
    evidence_deficit: float
    job_demand: float
    diminishing_returns: float
    actionability: float
    # One line per factor that materially moved the result, in a fixed order. These become the
    # "why is this prioritised?" bullets; they are built here, from the same numbers that drove
    # the decision, never generated afterwards from the conclusion.
    notes: List[str] = []


class Priority(BaseModel):
    model_config = ConfigDict(frozen=True)

    impact_band: str
    effort_band: str
    # Internal ordering value. Deliberately not part of what a client renders -- see the module
    # docstring. Kept on the model so tests and history can assert ordering directly.
    priority_score: float
    factors: PriorityFactors


def _effort_for(gap: StrategyGap) -> str:
    if gap.category == "SKILL_GAP" and gap.sub_kind == "ability":
        # They have the material; this is drilling, not learning from scratch.
        return "medium"
    return _EFFORT_BY_CATEGORY[gap.category]


def _impact_band(score: float) -> str:
    for threshold, band in _IMPACT_THRESHOLDS:
        if score >= threshold:
            return band
    return "low"


def _diminishing_factor(series: Optional[PerformanceSeries]) -> Tuple[float, Optional[str]]:
    """Damping from the student's own measured history, or none at all when there is not enough
    of it. `insufficient_history` deliberately returns 1.0 with no note: absence of history is
    not evidence of stability, and damping on it would quietly bury a real gap."""
    if series is None or not series.is_sufficient:
        return 1.0, None
    if series.verdict == "stable_high":
        return _STABLE_HIGH_FACTOR, (
            f"{series.subject} is already strong and steady across {series.observation_count} results, "
            "so more of the same is worth less than it looks."
        )
    if series.verdict == "improving":
        return _IMPROVING_FACTOR, f"{series.subject} is already trending upward without intervention."
    if series.verdict == "stable_low":
        return _STABLE_LOW_FACTOR, (
            f"{series.subject} has not moved across {series.observation_count} results, "
            "so repeating the same activity is unlikely to shift it."
        )
    return 1.0, None


def score(
    gap: StrategyGap,
    *,
    series: Optional[PerformanceSeries] = None,
    evidence_confidence: Optional[float] = None,
) -> Priority:
    """Rank one gap. Every input is a real signal; nothing here consults a clock or a model.

    `evidence_confidence` is evidence_engine's own value for the subject when the subject is a
    skill. It is passed in rather than looked up so this stays a pure function -- and so the
    confidence used is unambiguously the one the evidence engine published, not a second
    reading of it.
    """
    notes: List[str] = []

    # Role relevance. 1.0 for gaps that are not scoped to a role skill (a funnel or profile
    # problem is not more or less relevant depending on which role you want).
    if gap.role_weight is None:
        role_relevance = 1.0
    else:
        role_relevance = min(1.0, gap.role_weight / CORE_WEIGHT_THRESHOLD)
        if gap.role_weight >= CORE_WEIGHT_THRESHOLD:
            notes.append(f"{gap.subject} is a core skill for your target role.")
        elif role_relevance < 0.5:
            notes.append(f"{gap.subject} is a peripheral skill for your target role.")

    severity = _SEVERITY_FACTOR[gap.severity]

    # How far the evidence falls short. Unknown confidence is treated as a full deficit, which
    # is correct: we have nothing, and nothing is the largest possible shortfall -- not a
    # middling one, and certainly not a zero.
    evidence_deficit = 1.0 if evidence_confidence is None else max(0.0, 1.0 - evidence_confidence)

    # Real demand from the student's own tracked jobs, never a market statistic. A skill no
    # tracked job asks for gets no boost; one every tracked job asks for gets the full 1.5x.
    if gap.job_demand_share is None:
        job_demand = 1.0
    else:
        job_demand = 1.0 + (0.5 * gap.job_demand_share)
        if gap.job_demand_share >= 0.5:
            notes.append(
                f"{gap.subject} is missing from {gap.job_demand_share:.0%} of the jobs you are tracking."
            )

    diminishing, diminishing_note = _diminishing_factor(series)
    if diminishing_note:
        notes.append(diminishing_note)

    actionability = _ACTIONABILITY.get(gap.category, 1.0)
    if actionability < 1.0:
        notes.append("This is a real constraint, but not something you can change in the short term.")

    category_weight = _CATEGORY_WEIGHT[gap.category]
    priority_score = (
        role_relevance
        * category_weight
        * severity
        * evidence_deficit
        * job_demand
        * diminishing
        * actionability
    )
    effort_band = _effort_for(gap)
    impact_band = _impact_band(priority_score)

    if impact_band in ("high", "very_high") and effort_band == "low":
        notes.append("High impact for a small amount of work, which is why it is near the top.")

    return Priority(
        impact_band=impact_band,
        effort_band=effort_band,
        priority_score=priority_score,
        factors=PriorityFactors(
            role_relevance=role_relevance,
            category_weight=category_weight,
            severity=severity,
            evidence_deficit=evidence_deficit,
            job_demand=job_demand,
            diminishing_returns=diminishing,
            actionability=actionability,
            notes=notes,
        ),
    )


def rank(items: List[Tuple[StrategyGap, Priority]]) -> List[Tuple[StrategyGap, Priority]]:
    """Deterministic ordering: priority, then effort (cheaper first), then category, then subject.

    The full tie-break chain matters. Two gaps can genuinely produce the same score, and
    without the trailing keys their order would depend on dict iteration and the queue would
    reshuffle between two identical requests -- which would also make history snapshots
    disagree about what changed.
    """
    return sorted(
        items,
        key=lambda pair: (
            -pair[1].priority_score,
            _EFFORT_RANK[pair[1].effort_band],
            pair[0].category,
            pair[0].subject,
        ),
    )
