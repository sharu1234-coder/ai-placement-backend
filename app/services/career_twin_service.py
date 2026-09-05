"""Career Twin orchestration. Composes three existing/generic layers -- Evidence Engine,
Skill Graph, and weakness_service -- into one continuously-updated view of "how ready is this
student for their target role, and why". Deliberately independent of career_path/service.py's
own code (imports only its models and role_catalog, the same generic layer Skill Graph already
depends on) -- Career Twin reads Career Path data, it does not become part of Career Path.

Readiness is NEVER a naive average of raw scores (see the product's explicit "don't just
average every score" rule). Per skill, evidence_engine's confidence (0-1, itself already a
function of source breadth + score strength) is weighted by the skill's role importance
(skill_graph's weight) and averaged -- this measures "how much of what this role needs is
well-evidenced", which is a meaningfully different (and more honest) number than averaging
whatever raw test scores happen to exist. measured_score, confidence, and state stay separate,
visible fields per skill so "why this %?" is always answerable from real data, never a black
box.
"""

import datetime
from typing import Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.profile import StudentProfile
from app.models.readiness_snapshot import ReadinessSnapshot
from app.schemas.career_twin import CareerTwinRead, SkillTwinItemRead
from app.services import evidence_engine, notification_service
from app.services.role_context import resolve_role_context
from app.services.weakness_service import compute_weaknesses

_TREND_LOOKBACK_MIN_DAYS = 6  # "this week" -- see get_current's trend section
_MAX_WEAK_TAGS = 5


def _measured_score(items) -> Optional[float]:
    scored = [item.score for item in items if item.score is not None]
    return max(scored) if scored else None


def _pick_biggest_gap(skill_items: List[SkillTwinItemRead], weights: Dict[str, float]) -> Optional[str]:
    if not skill_items:
        return None
    # Lowest confidence first; among ties, the more heavily-weighted (more important) skill
    # first; among further ties, alphabetical -- fully deterministic, no hidden randomness.
    ranked = sorted(skill_items, key=lambda s: (s.confidence_percent, -weights.get(s.skill, 0.0), s.skill))
    return ranked[0].skill


def _build_reasons(display_role_name: str, skill_items: List[SkillTwinItemRead]) -> List[str]:
    total = len(skill_items)
    verified = sum(1 for s in skill_items if s.state == "verified")
    strong = sum(1 for s in skill_items if s.state == "strong")
    gaps = sum(1 for s in skill_items if s.state in ("claimed", "stale"))
    reasons = [f"{display_role_name} is your current target role.", f"{total} skills are weighted for this role."]
    if verified or strong:
        reasons.append(f"{verified} verified, {strong} strong out of {total}.")
    if gaps:
        reasons.append(f"{gaps} skill{'s' if gaps != 1 else ''} still need real evidence.")
    return reasons


def _upsert_snapshot(
    db: Session, profile_id: int, role_key: str, today: datetime.date, readiness_percent: int, per_skill: Dict[str, int]
) -> Optional[int]:
    """Returns the readiness_percent on record for this profile+role immediately BEFORE this
    write -- today's own prior value if this is a second read the same day, else the most
    recent earlier day's value, else None on a genuine first-ever read. This is the correct
    baseline for "did readiness just cross a milestone": comparing against it (rather than
    always against yesterday) means a second read later the same day never re-fires a
    milestone the first read already reported.

    Two concurrent first-reads-of-the-day for the same (profile, role) can both see no
    `existing` row and both attempt the insert branch; the table's own
    uq_readiness_snapshot_per_day constraint is the real guarantee, and a lost race turns into
    a clean re-fetch-and-update rather than an uncaught IntegrityError -- the identical shape
    as mission_service.get_or_create_today's create-race handling."""
    existing = (
        db.query(ReadinessSnapshot)
        .filter(
            ReadinessSnapshot.profile_id == profile_id,
            ReadinessSnapshot.role_family_key == role_key,
            ReadinessSnapshot.snapshot_date == today,
        )
        .first()
    )
    if existing is not None:
        baseline = existing.readiness_percent
        existing.readiness_percent = readiness_percent
        existing.per_skill_confidence_percent = per_skill
        db.commit()
        return baseline

    prior = (
        db.query(ReadinessSnapshot)
        .filter(
            ReadinessSnapshot.profile_id == profile_id,
            ReadinessSnapshot.role_family_key == role_key,
            ReadinessSnapshot.snapshot_date < today,
        )
        .order_by(ReadinessSnapshot.snapshot_date.desc())
        .first()
    )
    baseline = prior.readiness_percent if prior is not None else None
    db.add(
        ReadinessSnapshot(
            profile_id=profile_id,
            role_family_key=role_key,
            snapshot_date=today,
            readiness_percent=readiness_percent,
            per_skill_confidence_percent=per_skill,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Lost the create race to a concurrent request for the same (profile, role, day) --
        # fetch the winner's row and apply this call's values on top of it, same as the
        # existing-row branch above.
        db.rollback()
        winner = (
            db.query(ReadinessSnapshot)
            .filter(
                ReadinessSnapshot.profile_id == profile_id,
                ReadinessSnapshot.role_family_key == role_key,
                ReadinessSnapshot.snapshot_date == today,
            )
            .first()
        )
        winner.readiness_percent = readiness_percent
        winner.per_skill_confidence_percent = per_skill
        db.commit()
    return baseline


def _compute_trend(db: Session, profile_id: int, role_key: str, today: datetime.date, current_percent: int) -> Optional[int]:
    cutoff = today - datetime.timedelta(days=_TREND_LOOKBACK_MIN_DAYS)
    prior = (
        db.query(ReadinessSnapshot)
        .filter(
            ReadinessSnapshot.profile_id == profile_id,
            ReadinessSnapshot.role_family_key == role_key,
            ReadinessSnapshot.snapshot_date <= cutoff,
        )
        .order_by(ReadinessSnapshot.snapshot_date.desc())
        .first()
    )
    if prior is None:
        return None
    return current_percent - prior.readiness_percent


def get_current(db: Session, profile: StudentProfile) -> CareerTwinRead:
    context = resolve_role_context(db, profile)
    if context is None:
        return CareerTwinRead(
            has_role=False,
            role_source="none",
            reasons=["Select a target role to build your Career Twin."],
        )

    if not context.weights:
        return CareerTwinRead(
            has_role=True,
            role_family_key=context.role_family_key,
            display_role_name=context.display_role_name,
            role_source=context.role_source,
            is_estimate=context.is_estimate,
            reasons=["No required skills are defined for this role yet."],
        )

    skills = list(context.weights.keys())
    evidence_views = evidence_engine.get_all_skill_evidence(db, profile.id, skills)

    skill_items = [
        SkillTwinItemRead(
            skill=view.skill,
            weight=context.weights[view.skill],
            measured_score=_measured_score(view.items),
            confidence_percent=round(view.confidence * 100),
            state=view.state,
            last_verified_at=view.last_verified_at,
        )
        for view in evidence_views
    ]

    total_weight = sum(context.weights.values())
    readiness_percent = (
        round(100 * sum(context.weights[s.skill] * (s.confidence_percent / 100) for s in skill_items) / total_weight)
        if total_weight
        else 0
    )

    today = datetime.date.today()
    per_skill_confidence = {s.skill: s.confidence_percent for s in skill_items}
    baseline_percent = _upsert_snapshot(db, profile.id, context.role_family_key, today, readiness_percent, per_skill_confidence)
    trend = _compute_trend(db, profile.id, context.role_family_key, today, readiness_percent)

    notification_service.check_readiness_milestone(
        db, profile.id, context.display_role_name, baseline_percent, readiness_percent
    )
    notification_service.check_evidence_transitions(db, profile.id, {s.skill: s.state for s in skill_items})
    db.commit()

    biggest_gap = _pick_biggest_gap(skill_items, context.weights)
    next_action_hint = None
    if biggest_gap is not None:
        gap_item = next(s for s in skill_items if s.skill == biggest_gap)
        next_action_hint = evidence_engine.next_action_hint(biggest_gap, gap_item.state)

    weaknesses = compute_weaknesses(db, profile.id, jd_id=None)

    return CareerTwinRead(
        has_role=True,
        role_family_key=context.role_family_key,
        display_role_name=context.display_role_name,
        role_source=context.role_source,
        readiness_percent=readiness_percent,
        readiness_trend_percent=trend,
        is_estimate=context.is_estimate,
        reasons=_build_reasons(context.display_role_name, skill_items),
        skills=skill_items,
        biggest_gap_skill=biggest_gap,
        next_action_hint=next_action_hint,
        weak_skill_tags=[w.skill_tag for w in weaknesses[:_MAX_WEAK_TAGS]],
    )
