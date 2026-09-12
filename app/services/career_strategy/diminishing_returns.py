"""Performance *series* analysis -- the input Phase 2 needs to stop recommending an area the
student has already mastered, and to notice one they keep practising without improving.

A single latest score cannot answer either question. 92% today could be a first attempt or the
fifth in a row; 48% today could be a plateau or the middle of a steep climb. Both readings lead
to opposite advice, so this module reads the whole ordered series and refuses to answer when
there is not enough of it.

**The refusal is the point.** Below `MIN_OBSERVATIONS` the verdict is `insufficient_history`
and every derived statistic is `None` -- no mean, no variance, no slope. Two data points can
always be joined by a line, and reporting that line as a trend would be a fabricated
statistical conclusion dressed up as a measurement. This is the same discipline
weakness_service applies when it declines to invent a score for an unmeasured weakness, and
that job_hunt/readiness.py applies when it names a dimension `unknown` rather than scoring it.

Nothing here decides anything. It produces a verdict per skill; Phase 2 turns
`stable_high` into a de-prioritisation and `stable_low` into a "what you are doing is not
working" signal. The "Don't do this yet" queue is explicitly not built here.

Pure: no database access, no clock read, no AI. Given the same `AssessmentResult` rows it
returns the same output every time, which is what makes §68's determinism requirement testable
rather than aspirational.
"""

import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict

from app.models.assessment_result import AssessmentResult
from app.services.career_strategy.signals import SignalBundle
from app.services.skill_matching import skill_present

# Three is the smallest series that can distinguish a trend from a pair of points: with two
# observations every series is perfectly linear and "improving"/"declining" is guaranteed,
# which makes the verdict meaningless rather than merely uncertain.
MIN_OBSERVATIONS = 3

# Variance at or below this counts as stable. Scores are 0-1, so 0.01 is a standard deviation
# of 0.1 -- ten percentage points of wobble, which is ordinary test-to-test noise rather than a
# real change in ability.
STABLE_VARIANCE = 0.01

# Mean at or above this, while stable, is "already good enough that more practice here buys
# little". Matches evidence_engine's own _STRONG_SCORE_THRESHOLD of 75/100 so the two layers
# agree about what "strong" means.
HIGH_MEAN = 0.75

# Mean below this, while stable, is the repeated-practice-without-improvement case.
LOW_MEAN = 0.6

# Slope per observation. 0.02 over a series means roughly two points of gain per attempt --
# small, but consistently directional. Below it in absolute terms the series is treated as
# having no trend rather than a trivial one.
TREND_EPSILON = 0.02

VERDICTS: Tuple[str, ...] = (
    "insufficient_history",
    "improving",
    "declining",
    "stable_high",
    "stable_low",
    "volatile",
)


class PerformanceSeries(BaseModel):
    """One skill's ordered score history and what, if anything, can be concluded from it."""

    model_config = ConfigDict(frozen=True)

    subject: str
    # Oldest first. Normalised 0-1, straight from AssessmentResult.skill_scores.
    observations: List[float]
    observed_at: List[Optional[datetime.datetime]]
    observation_count: int
    # False below MIN_OBSERVATIONS. When False, every statistic below is None -- not zero,
    # not a default. Callers must branch on this rather than reading a placeholder.
    is_sufficient: bool
    verdict: str
    mean: Optional[float] = None
    variance: Optional[float] = None
    # Least-squares slope per observation index. Positive means improving.
    trend: Optional[float] = None
    latest: Optional[float] = None
    reason: str = ""


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _variance(values: Sequence[float]) -> float:
    """Population variance. The series is the complete set of attempts this student made, not
    a sample drawn from a larger population, so dividing by n is correct and the n-1 sample
    correction would be the wrong estimator here."""
    mu = _mean(values)
    return sum((value - mu) ** 2 for value in values) / len(values)


def _slope(values: Sequence[float]) -> float:
    """Ordinary least-squares slope against the observation index.

    Index rather than elapsed time on purpose: the question is "is the student getting better
    each time they try", and spacing the x-axis by calendar gaps would make an identical run of
    scores read as improving or declining depending only on when they happened to sit the
    tests. Hand-rolled rather than pulled from numpy/statistics -- six lines, no new dependency,
    and identical results across platforms.
    """
    n = len(values)
    mean_x = (n - 1) / 2
    mean_y = _mean(values)
    numerator = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
    denominator = sum((index - mean_x) ** 2 for index in range(n))
    if denominator == 0:  # pragma: no cover -- unreachable for n >= MIN_OBSERVATIONS
        return 0.0
    return numerator / denominator


def _classify(values: Sequence[float]) -> Tuple[str, str]:
    """Verdict plus a one-line reason, in a fixed order of checks.

    Stability is tested before direction because a flat-but-strong series and a flat-but-weak
    one both have a near-zero slope, and calling either "improving" or "declining" on the sign
    of statistical noise would be exactly the false conclusion this module exists to avoid.
    """
    mean = _mean(values)
    variance = _variance(values)
    slope = _slope(values)

    if variance <= STABLE_VARIANCE:
        if mean >= HIGH_MEAN:
            return (
                "stable_high",
                f"{len(values)} consistent results averaging {mean:.0%} — already a strength.",
            )
        if mean < LOW_MEAN:
            return (
                "stable_low",
                f"{len(values)} results averaging {mean:.0%} with little movement — repeated practice is not shifting it.",
            )
        return ("volatile", f"{len(values)} steady results averaging {mean:.0%}, neither strong nor clearly stuck.")

    if slope >= TREND_EPSILON:
        return ("improving", f"{len(values)} results trending upward — current approach is working.")
    if slope <= -TREND_EPSILON:
        return ("declining", f"{len(values)} results trending downward.")
    return ("volatile", f"{len(values)} results with no consistent direction.")


def _insufficient(subject: str, values: List[float], dates: List[Optional[datetime.datetime]]) -> PerformanceSeries:
    return PerformanceSeries(
        subject=subject,
        observations=values,
        observed_at=dates,
        observation_count=len(values),
        is_sufficient=False,
        verdict="insufficient_history",
        latest=values[-1] if values else None,
        reason=(
            f"Only {len(values)} result{'s' if len(values) != 1 else ''} recorded; "
            f"{MIN_OBSERVATIONS} are needed before a trend means anything."
        ),
    )


def series_for(
    subject: str, results: Sequence[AssessmentResult]
) -> PerformanceSeries:
    """Build one skill's series from assessment results already ordered oldest-first.

    Matching uses skill_matching.skill_present, the same token-subset match the rest of the
    product uses, so "SQL" finds a result keyed "sql" and this module does not become a second
    place where skill-name matching is decided.
    """
    values: List[float] = []
    dates: List[Optional[datetime.datetime]] = []
    for result in results:
        for tag, score in (result.skill_scores or {}).items():
            if skill_present(subject, [tag]):
                values.append(float(score))
                dates.append(result.created_at)
                break

    if len(values) < MIN_OBSERVATIONS:
        return _insufficient(subject, values, dates)

    verdict, reason = _classify(values)
    return PerformanceSeries(
        subject=subject,
        observations=values,
        observed_at=dates,
        observation_count=len(values),
        is_sufficient=True,
        verdict=verdict,
        mean=_mean(values),
        variance=_variance(values),
        trend=_slope(values),
        latest=values[-1],
        reason=reason,
    )


def analyse(bundle: SignalBundle) -> Dict[str, PerformanceSeries]:
    """Every skill in the bundle's universe, keyed by skill.

    Interview and recruiter-attack dimension tags are not in that universe (see
    signals._skill_universe), so they cannot leak in here -- their cross-session history is
    Interview Memory's job, and it already has a purpose-built status machine for it rather
    than a score series.
    """
    return {skill: series_for(skill, bundle.assessment_results) for skill in bundle.skills}
