"""Career Decision Lab -- a pure, stateless "what if" simulation comparing 2-3 candidate
roles side by side. Reuses career_twin_service's own readiness math (a weighted average of
Evidence Engine confidence by Skill Graph weight -- see that module's docstring for why this
is never a naive score average), parameterized by an arbitrary candidate role instead of only
the student's current target role. Nothing here is persisted -- there's nothing to own, per
the architecture plan's explicit decision for this feature; every candidate role is
recomputed live on each call from the same underlying data Career Twin already reads.

Deliberately never returns anything resembling a probability of employment (the product's
explicit red line for this feature) -- CareerDecisionRoleRead/CareerDecisionSimulationRead
have no such field at all, so there is nothing to leak even by accident. What it returns
instead: a readiness percent (the same honest metric Career Twin already shows), a
best_fit/alternative/stretch classification against fixed, documented thresholds (evaluated
against each role on its own terms, not relative to the other candidates -- so a role's
classification never flips just because a weaker option happened to be compared alongside
it), and an explicit, clearly labeled effort estimate in hours (is_estimate is always True,
and `assumptions` spells out exactly what the estimate does and doesn't account for).
"""

import math
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.role import Role
from app.schemas.career_decision import CareerDecisionRoleRead, CareerDecisionSimulationRead
from app.services import evidence_engine, skill_graph

_BEST_FIT_THRESHOLD = 70
_ALTERNATIVE_THRESHOLD = 40

_CLASSIFICATION_LABELS = {"best_fit": "Best Fit", "alternative": "Alternative", "stretch": "Stretch"}

# Coarse, documented estimate: roughly how many focused hours it takes to move one entirely
# unevidenced skill to fully verified. Scaled by the skill's role weight (a heavily-weighted
# skill costs proportionally more) and by how much of it is *already* evidenced (1 -
# confidence) -- a skill already near-verified costs almost nothing more. This is explicitly
# a rough planning number, never presented as a guarantee (see `assumptions` in the response).
_BASE_HOURS_PER_SKILL = 15.0

# "Unrealistic hours" clamping, per the plan's explicit test requirement -- input is clamped
# to a sane range rather than rejected, since a slider-driven UI input should never hard-error.
_MIN_WEEKLY_HOURS = 1
_MAX_WEEKLY_HOURS = 80

_ASSUMPTIONS = [
    "Hours are a rough estimate of how much of each role's required skills are not yet "
    "backed by real evidence -- not a guarantee, and not how long any specific person will "
    "actually take.",
    "Assumes focused, consistent study/practice time, not calendar time.",
    "Classification (Best Fit / Alternative / Stretch) reflects your current readiness for "
    "each role on its own, not a ranking against the other roles you compared.",
]


class InvalidRoleError(Exception):
    """A requested role_id does not exist."""


def _clamp_weekly_hours(weekly_hours: Optional[int]) -> Optional[int]:
    if weekly_hours is None:
        return None
    return max(_MIN_WEEKLY_HOURS, min(_MAX_WEEKLY_HOURS, weekly_hours))


def _classify(readiness_percent: int) -> str:
    if readiness_percent >= _BEST_FIT_THRESHOLD:
        return "best_fit"
    if readiness_percent >= _ALTERNATIVE_THRESHOLD:
        return "alternative"
    return "stretch"


def _evaluate_role(
    db: Session, profile_id: int, role: Role, weekly_hours_used: Optional[int]
) -> Tuple[CareerDecisionRoleRead, Dict[str, float]]:
    weights = skill_graph.get_role_skill_weights_by_role_id(db, role.id)
    if not weights:
        return (
            CareerDecisionRoleRead(
                role_id=role.id, display_role_name=role.name, readiness_percent=0, classification="stretch",
                estimated_hours_to_strengthen=0.0,
                estimated_weeks=(0 if weekly_hours_used is not None else None),
                reasons=["No required skills are defined for this role yet."],
            ),
            {},
        )

    skills = list(weights.keys())
    evidence_views = evidence_engine.get_all_skill_evidence(db, profile_id, skills)
    total_weight = sum(weights.values())
    readiness_percent = (
        round(100 * sum(weights[v.skill] * v.confidence for v in evidence_views) / total_weight)
        if total_weight
        else 0
    )

    gap_hours = round(
        sum(_BASE_HOURS_PER_SKILL * weights[v.skill] * (1 - v.confidence) for v in evidence_views), 1
    )
    estimated_weeks: Optional[int] = None
    if weekly_hours_used is not None:
        estimated_weeks = math.ceil(gap_hours / weekly_hours_used) if gap_hours > 0 else 0

    # Lowest confidence first; among ties, the more heavily-weighted skill first; among
    # further ties, alphabetical -- identical tie-break rule to career_twin_service's
    # _pick_biggest_gap, so the two features never disagree about which skill matters most.
    ranked_gaps = sorted(evidence_views, key=lambda v: (v.confidence, -weights[v.skill], v.skill))
    biggest_gap = ranked_gaps[0].skill if ranked_gaps else None

    classification = _classify(readiness_percent)
    verified = sum(1 for v in evidence_views if v.state == "verified")
    strong = sum(1 for v in evidence_views if v.state == "strong")
    gaps = sum(1 for v in evidence_views if v.state in ("claimed", "stale"))
    reasons = [
        f"{readiness_percent}% of {role.name}'s required skills are backed by real evidence.",
        f"{verified} verified, {strong} strong out of {len(evidence_views)} skills.",
    ]
    if gaps:
        reasons.append(f"{gaps} skill{'s' if gaps != 1 else ''} still need real evidence.")
    reasons.append(
        f"Classified as {_CLASSIFICATION_LABELS[classification]} at the "
        f"{_BEST_FIT_THRESHOLD}%/{_ALTERNATIVE_THRESHOLD}% readiness thresholds."
    )

    return (
        CareerDecisionRoleRead(
            role_id=role.id, display_role_name=role.name, readiness_percent=readiness_percent,
            classification=classification, biggest_gap_skill=biggest_gap,
            estimated_hours_to_strengthen=gap_hours, estimated_weeks=estimated_weeks, reasons=reasons,
        ),
        weights,
    )


def simulate(
    db: Session, profile_id: int, role_ids: List[int], weekly_hours: Optional[int]
) -> CareerDecisionSimulationRead:
    weekly_hours_used = _clamp_weekly_hours(weekly_hours)

    roles: List[Role] = []
    for role_id in role_ids:
        role = db.get(Role, role_id)
        if role is None:
            raise InvalidRoleError(f"Role {role_id} not found")
        roles.append(role)

    results: List[CareerDecisionRoleRead] = []
    all_weights: List[Dict[str, float]] = []
    for role in roles:
        read, weights = _evaluate_role(db, profile_id, role, weekly_hours_used)
        results.append(read)
        all_weights.append(weights)

    # Shared skills: required by 2+ of the compared roles -- work on these benefits every
    # path it's shared with, useful context alongside the per-role breakdown.
    skill_counts: Dict[str, int] = {}
    for weights in all_weights:
        for skill in weights:
            skill_counts[skill] = skill_counts.get(skill, 0) + 1
    shared_skills = sorted(skill for skill, count in skill_counts.items() if count >= 2)

    ranked = sorted(results, key=lambda r: -r.readiness_percent)

    return CareerDecisionSimulationRead(
        roles=ranked, shared_skills=shared_skills, weekly_hours_used=weekly_hours_used,
        assumptions=list(_ASSUMPTIONS),
    )
