"""Cross-source mismatch detection -- the nine places where two true facts point opposite ways.

Most of what this layer does is aggregate. This module does the opposite: it looks for pairs of
facts that *disagree*, because a disagreement is usually more informative than either fact
alone. A student with verified Airflow evidence and no Airflow on their resume does not have an
Airflow problem; they have a resume problem, and only the mismatch reveals which.

Three rules govern every detector here:

**Both facts survive.** A contradiction records `left_*` and `right_*` and overwrites neither.
precedence.py can say which one to *act on*, but the losing fact stays visible and stays
citable -- that is the whole reason `PrecedenceResolution` keeps `overridden_keys`. Silently
discarding the weaker side would turn "your resume says more than your evidence shows" into
"you don't know AWS", which is a different and unsupported claim.

**Nothing accuses.** The student is not lying. A resume claim outrunning available evidence is
usually a resume written before the evidence existed, or work the product simply cannot see.
Every `explanation` is phrased as an observation about what *we can see*, never about their
honesty -- the spec's own wording ("your resume currently presents AWS strongly, but your
available supporting evidence is limited") is the register.

**Detection requires both sides.** No contradiction fires from an absence. If there is no
resume, no ATS check, or no interview history, the detectors that need them return nothing
rather than treating the gap as one half of a mismatch.

`reclassify_to` is advisory, permanently and by design -- not an unfinished wire. It records
how *this contradiction* reads the subject ("verified evidence with no resume mention is a
resume problem, not a skill problem"), and it is surfaced to the client rather than applied to
the gap.

Resist the urge to "finish" it by feeding it back into gaps.classify. That is not a missing
step; it is the thing this field must never do. gaps.py already derives the category
independently from the same underlying signals, and a second path that rewrote a gap's category
after the fact would leave the gap list disagreeing with the reason printed beside it -- two
answers from one system, which is exactly what this layer exists to prevent. What actually
stops a proven skill being recommended is the evidence check in `_classify_skill`, not this
hint. The test suite pins the two vocabularies together (every value must name a real
GAP_CATEGORY) so the advisory reading and the authoritative one cannot drift apart.

Pure and deterministic -- no clock, no database, no AI.
"""

from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

from app.services.career_strategy import diminishing_returns
from app.services.career_strategy import signals as signals_module
from app.services.career_strategy.gaps import (
    SEVERE_SCORE_THRESHOLD,
    WEAK_PROGRESSION_RATE,
    WEAK_SCORE_THRESHOLD,
)
from app.services.career_strategy.signals import SignalBundle
from app.services.skill_matching import skill_present

CONTRADICTION_KINDS: Tuple[str, ...] = (
    "RESUME_CLAIM_EXCEEDS_EVIDENCE",
    "TECHNICAL_STRONG_COMMUNICATION_WEAK",
    "LEARNING_AHEAD_OF_PERFORMANCE",
    "LEARNING_WITHOUT_EVIDENCE",
    "RESUME_RICH_ATS_WEAK",
    "CAPABILITY_OUTSIDE_TARGET_ROLE",
    "PRACTICE_WITHOUT_IMPROVEMENT",
    "APPLICATION_WITHOUT_PROGRESSION",
    "MATCH_HIGH_READINESS_UNKNOWN",
)

# Learning counted as substantially complete. Not 1.0: a student who has finished four of five
# roadmap items on a skill and still tests poorly has the same problem as one who finished all
# five, and demanding perfection here would hide the common case.
LEARNING_COMPLETE_SHARE = 0.75

# An ATS overall score below this is weak enough to be worth surfacing against a resume that
# is otherwise full of content.
ATS_WEAK_THRESHOLD = 0.6

# A resume carrying at least this many detected skills is "rich" -- enough content that a low
# ATS score is about presentation rather than about an empty document.
RESUME_RICH_SKILL_COUNT = 5

# A tracked job scoring at or above this is a genuinely good fit, making unknown application
# readiness the more urgent of the two facts.
STRONG_MATCH_THRESHOLD = 0.7

# Minimum role skills that must be at `claimed` before "your strengths are elsewhere" is a fair
# reading rather than an artefact of a role with one or two skills defined.
_MIN_ROLE_SKILLS_FOR_ALIGNMENT = 3


class StrategyContradiction(BaseModel):
    """Two facts that disagree, both preserved, with a factual explanation of the tension."""

    model_config = ConfigDict(frozen=True)

    kind: str
    subject: Optional[str] = None
    left_statement: str = ""
    left_signal_keys: List[str] = []
    right_statement: str = ""
    right_signal_keys: List[str] = []
    explanation: str = ""
    # Gap category this subject should be re-read as, when the mismatch changes the answer.
    # None when the contradiction is informative but does not change the classification.
    reclassify_to: Optional[str] = None


def _resume_claim_exceeds_evidence(bundle: SignalBundle) -> List[StrategyContradiction]:
    """#1 -- the resume names a skill that nothing independent supports.

    Requires an analysed resume; without one there is no claim to exceed anything. Fires only
    where every piece of evidence for the skill is self-asserted, which is exactly
    evidence_engine's own definition of "the student told us and nobody checked".
    """
    if bundle.resume_skills is None:
        return []

    found: List[StrategyContradiction] = []
    for skill in bundle.skills:
        if not skill_present(skill, list(bundle.resume_skills)):
            continue
        view = bundle.evidence_by_skill.get(skill)
        if view is None:
            continue
        independent = [item for item in view.items if not item.self_asserted]
        if independent:
            continue
        found.append(
            StrategyContradiction(
                kind="RESUME_CLAIM_EXCEEDS_EVIDENCE",
                subject=skill,
                left_statement=f"Your resume presents {skill} as one of your skills.",
                left_signal_keys=[f"resume:{skill}", "resume:on_file"],
                right_statement=f"Nothing independent in your profile evidences {skill} yet.",
                right_signal_keys=[f"evidence:{skill}"],
                explanation=(
                    f"Your resume currently presents {skill}, but the supporting evidence we can see is limited. "
                    "Building one piece of independent proof would make the claim hold up under questioning."
                ),
                reclassify_to="EVIDENCE_GAP",
            )
        )
    return found


def _technical_strong_communication_weak(bundle: SignalBundle) -> List[StrategyContradiction]:
    """#2 -- solid technical evidence alongside weak delivery.

    Uses only the delivery-facing interview dimensions (clarity, structure) plus Recruiter
    Attack's communication score. A low technical_correctness is a technical problem and is
    already an ABILITY_GAP; conflating the two would send a student to communication practice
    for a knowledge gap.
    """
    strong_skills = sorted(
        skill
        for skill, view in bundle.evidence_by_skill.items()
        if view.state in ("strong", "verified")
    )
    if not strong_skills:
        return []

    weak: List[Tuple[str, float]] = []
    for dimension in signals_module.COMMUNICATION_DIMENSIONS:
        signal = bundle.by_key(f"interview:{dimension}")
        if signal is not None and signal.value is not None and signal.value < WEAK_SCORE_THRESHOLD:
            weak.append((f"interview:{dimension}", signal.value))
    attack = bundle.by_key("recruiter_attack:communication")
    if attack is not None and attack.value is not None and attack.value < WEAK_SCORE_THRESHOLD:
        weak.append(("recruiter_attack:communication", attack.value))

    if not weak:
        return []

    keys = sorted(key for key, _value in weak)
    lowest = min(value for _key, value in weak)
    return [
        StrategyContradiction(
            kind="TECHNICAL_STRONG_COMMUNICATION_WEAK",
            subject=None,
            left_statement=f"You have strong or verified evidence in {len(strong_skills)} skills.",
            left_signal_keys=[f"evidence:{skill}" for skill in strong_skills],
            right_statement=f"Your interview delivery scores as low as {lowest:.0%}.",
            right_signal_keys=keys,
            explanation=(
                "Your technical evidence is stronger than the way it currently comes across in interviews. "
                "The work is there; practising how you explain it is likely to move you further than more study."
            ),
            reclassify_to="INTERVIEW_GAP",
        )
    ]


def _learning_ahead_of_performance(bundle: SignalBundle) -> List[StrategyContradiction]:
    """#3 -- roadmap says learned, measurement says otherwise.

    Both facts are real and neither is discarded: the topics genuinely were completed, and the
    score genuinely was low. What it changes is the recommendation -- more of the same learning
    is unlikely to help, so this reclassifies toward practice.
    """
    found: List[StrategyContradiction] = []
    for skill in bundle.skills:
        learning = bundle.by_key(f"learning:{skill}")
        if learning is None or learning.value is None or learning.value < LEARNING_COMPLETE_SHARE:
            continue
        measured = [
            signal
            for signal in bundle.for_subject(skill)
            if signal.source in ("assessment", "practice") and signal.value is not None
        ]
        if not measured:
            continue
        worst = min(measured, key=lambda signal: signal.value)
        if worst.value >= WEAK_SCORE_THRESHOLD:
            continue
        found.append(
            StrategyContradiction(
                kind="LEARNING_AHEAD_OF_PERFORMANCE",
                subject=skill,
                left_statement=f"You have completed {learning.value:.0%} of your {skill} learning items.",
                left_signal_keys=[learning.key],
                right_statement=f"Your measured {skill} performance is {worst.value:.0%}.",
                right_signal_keys=[worst.key],
                explanation=(
                    f"You have worked through the {skill} material, but it is not showing up in your results yet. "
                    "That usually points to practice rather than more reading."
                ),
                reclassify_to="SKILL_GAP",
            )
        )
    return found


def _learning_without_evidence(bundle: SignalBundle) -> List[StrategyContradiction]:
    """#4 -- time going into learning that never turns into proof.

    Distinct from LEARNING_AHEAD_OF_PERFORMANCE above, and mutually exclusive with it by
    construction: that one needs a measurement to compare against, and a measurement is itself
    independent evidence, so a skill cannot satisfy both. This is the quieter and more common
    case -- the student finishes the material and nothing in the product can show they can do
    it, so a recruiter has nothing to look at either.

    The framing matters. The student has not wasted their time; they have done the first half
    of the work. The recommendation is to convert it, not to redo it.
    """
    found: List[StrategyContradiction] = []
    for skill in bundle.skills:
        learning = bundle.by_key(f"learning:{skill}")
        if learning is None or learning.value is None or learning.value < LEARNING_COMPLETE_SHARE:
            continue
        view = bundle.evidence_by_skill.get(skill)
        if view is None:
            continue
        if any(not item.self_asserted for item in view.items):
            continue
        found.append(
            StrategyContradiction(
                kind="LEARNING_WITHOUT_EVIDENCE",
                subject=skill,
                left_statement=f"You have completed {learning.value:.0%} of your {skill} learning items.",
                left_signal_keys=[learning.key],
                right_statement=f"Nothing in your profile yet demonstrates {skill}.",
                right_signal_keys=[f"evidence:{skill}"],
                explanation=(
                    f"You are investing learning time in {skill} without converting it into proof. "
                    "One project or assessment would turn the work you have already done into evidence "
                    "a recruiter can see."
                ),
                reclassify_to="EVIDENCE_GAP",
            )
        )
    return found


def _application_without_progression(bundle: SignalBundle) -> List[StrategyContradiction]:
    """#8 -- effort going into applications that are not converting.

    Reads the tracker's progression rate, which signals reports as unknown until enough
    applications exist for a ratio to mean anything. So this cannot fire on a student who has
    applied twice and heard nothing yet -- which is not a funnel problem, just a normal wait.

    Deliberately says "may indicate" and names no cause. The tracker knows that applications
    are not progressing; it does not know why, and the honest move is to point at the thing
    that is measurable rather than assert a reason the data does not establish.
    """
    rate = bundle.by_key("tracker:progression_rate")
    applied = bundle.by_key("tracker:applied")
    if rate is None or rate.value is None or applied is None:
        return []
    if rate.value > WEAK_PROGRESSION_RATE:
        return []
    return [
        StrategyContradiction(
            kind="APPLICATION_WITHOUT_PROGRESSION",
            subject=None,
            left_statement=applied.raw_label,
            left_signal_keys=["tracker:applied"],
            right_statement=rate.raw_label,
            right_signal_keys=["tracker:progression_rate"],
            explanation=(
                "Your application activity is high, but progression is weak. This may indicate that the "
                "profile reaching employers is not yet showing what your evidence shows - worth closing "
                "your largest gap before increasing volume further."
            ),
            reclassify_to=None,
        )
    ]


def _resume_rich_ats_weak(bundle: SignalBundle) -> List[StrategyContradiction]:
    """#4 -- a full resume that the ATS check scores poorly.

    Needs both an analysed resume with real content and an actual ATS check. Without the check
    there is no second fact, and guessing at ATS alignment from the skill count alone would be
    inventing the very number ats_scoring exists to measure.
    """
    if bundle.resume_skills is None or len(bundle.resume_skills) < RESUME_RICH_SKILL_COUNT:
        return []
    ats = bundle.by_key("ats:overall")
    if ats is None or ats.value is None or ats.value >= ATS_WEAK_THRESHOLD:
        return []

    return [
        StrategyContradiction(
            kind="RESUME_RICH_ATS_WEAK",
            subject=None,
            left_statement=f"Your resume lists {len(bundle.resume_skills)} skills.",
            left_signal_keys=["resume:on_file"],
            right_statement=f"Your most recent ATS check scored {ats.value:.0%}.",
            right_signal_keys=["ats:overall"],
            explanation=(
                "Your resume has plenty of content, but it is not reaching an automated screen well. "
                "This is a formatting and positioning problem rather than a missing-experience one."
            ),
            reclassify_to="RESUME_GAP",
        )
    ]


def _capability_outside_target_role(bundle: SignalBundle) -> List[StrategyContradiction]:
    """#5 -- real strengths, pointed somewhere other than the stated target.

    Only fires when the role defines enough skills for the comparison to mean something, and
    when strong evidence genuinely exists outside that set. This is the honest version of
    "should you be aiming at a different role?" -- it states both facts and leaves the
    conclusion to Career Decision Lab, which already owns multi-target comparison.
    """
    if bundle.role is None:
        return []
    weights = bundle.role.weights
    if len(weights) < _MIN_ROLE_SKILLS_FOR_ALIGNMENT:
        return []

    role_skills = [skill for skill in bundle.skills if skill in weights]
    outside_strong = sorted(
        skill
        for skill in bundle.skills
        if skill not in weights
        and (view := bundle.evidence_by_skill.get(skill)) is not None
        and view.state in ("strong", "verified")
    )
    if not outside_strong:
        return []

    unevidenced = [
        skill
        for skill in role_skills
        if (view := bundle.evidence_by_skill.get(skill)) is not None and view.state == "claimed"
    ]
    if len(unevidenced) < len(role_skills) / 2:
        return []

    return [
        StrategyContradiction(
            kind="CAPABILITY_OUTSIDE_TARGET_ROLE",
            subject=None,
            left_statement=f"You have strong evidence in {', '.join(outside_strong)}.",
            left_signal_keys=[f"evidence:{skill}" for skill in outside_strong],
            right_statement=(
                f"{len(unevidenced)} of {len(role_skills)} skills weighted for "
                f"{bundle.role.display_role_name} have no evidence yet."
            ),
            right_signal_keys=[f"evidence:{skill}" for skill in sorted(unevidenced)],
            explanation=(
                "Your demonstrated strengths sit outside what your current target role weights most heavily. "
                "That is worth a deliberate decision — either build toward this role, or compare it against "
                "one your existing evidence already fits."
            ),
            reclassify_to="JOB_TARGETING_GAP",
        )
    ]


def _practice_without_improvement(
    bundle: SignalBundle, series: Optional[Dict[str, diminishing_returns.PerformanceSeries]] = None
) -> List[StrategyContradiction]:
    """#6 -- repeated attempts, no measurable movement.

    Reads the *series*, never the latest score, and inherits diminishing_returns' refusal to
    conclude anything below MIN_OBSERVATIONS. `stable_low` is precisely this contradiction:
    enough attempts to be sure, low enough to matter, flat enough that the current approach is
    demonstrably not the thing that will fix it.
    """
    series = series if series is not None else diminishing_returns.analyse(bundle)
    found: List[StrategyContradiction] = []
    for skill in sorted(series):
        entry = series[skill]
        if not entry.is_sufficient or entry.verdict != "stable_low":
            continue
        found.append(
            StrategyContradiction(
                kind="PRACTICE_WITHOUT_IMPROVEMENT",
                subject=skill,
                left_statement=f"You have {entry.observation_count} recorded {skill} results.",
                left_signal_keys=[f"assessment:{skill}"],
                right_statement=f"They average {entry.mean:.0%} with little movement between them.",
                right_signal_keys=[f"evidence:{skill}"],
                explanation=(
                    f"Repeating the same {skill} practice is not shifting your results. "
                    "A different format — a guided project, or working through the underlying concept — "
                    "is more likely to move it than another round of the same thing."
                ),
                reclassify_to="SKILL_GAP" if entry.mean is not None and entry.mean < SEVERE_SCORE_THRESHOLD else None,
            )
        )
    return found


def _match_high_readiness_unknown(bundle: SignalBundle) -> List[StrategyContradiction]:
    """#7 -- a job that fits, with application readiness we cannot judge.

    Uses the authoritative JobListingMatch score and the readiness *inputs* only. It does not
    call readiness.compute and does not estimate a readiness number -- the contradiction is
    precisely that readiness is unknown, and manufacturing a figure would destroy the very fact
    being reported.
    """
    strong = [
        signal
        for signal in bundle.signals
        if signal.key.startswith("job_match:") and signal.value is not None and signal.value >= STRONG_MATCH_THRESHOLD
    ]
    if not strong:
        return []
    missing = [
        signal
        for signal in bundle.by_category("application_readiness")
        if signal.evidence_kind == "missing"
    ]
    if not missing:
        return []

    best = max(strong, key=lambda signal: (signal.value, signal.key))
    return [
        StrategyContradiction(
            kind="MATCH_HIGH_READINESS_UNKNOWN",
            subject=best.subject,
            left_statement=f"{best.raw_label} — one of your tracked jobs is a strong match.",
            left_signal_keys=[best.key],
            right_statement="; ".join(signal.raw_label for signal in missing),
            right_signal_keys=sorted(signal.key for signal in missing),
            explanation=(
                "You are tracking a job that matches you well, but part of your application readiness "
                "cannot be assessed yet. Closing that first makes the application stronger than finding more jobs."
            ),
            reclassify_to="PROFILE_GAP",
        )
    ]


def detect(
    bundle: SignalBundle, series: Optional[Dict[str, diminishing_returns.PerformanceSeries]] = None
) -> Tuple[StrategyContradiction, ...]:
    """All seven detectors, in a fixed order.

    `series` is accepted so a caller that already ran diminishing_returns.analyse does not pay
    for it twice; omitted, it is computed here. Either way the result is identical -- the
    analysis is pure.
    """
    found: List[StrategyContradiction] = []
    found += _resume_claim_exceeds_evidence(bundle)
    found += _technical_strong_communication_weak(bundle)
    found += _learning_ahead_of_performance(bundle)
    found += _learning_without_evidence(bundle)
    found += _resume_rich_ats_weak(bundle)
    found += _capability_outside_target_role(bundle)
    found += _practice_without_improvement(bundle, series)
    found += _application_without_progression(bundle)
    found += _match_high_readiness_unknown(bundle)

    # Stable ordering by (kind, subject) so identical inputs give an identical list regardless
    # of the order the detectors happened to append in.
    return tuple(sorted(found, key=lambda item: (CONTRADICTION_KINDS.index(item.kind), item.subject or "")))
