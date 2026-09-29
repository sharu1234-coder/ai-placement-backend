"""Career Twin orchestration. Composes three existing/generic layers -- Evidence Engine,
Skill Graph, and weakness_service -- into one continuously-updated view of "how ready is this
student for their target role, and why". Deliberately independent of career_path/service.py's
own code (imports only its models and role_catalog, the same generic layer Skill Graph already
depends on) -- Career Twin reads Career Path data, it does not become part of Career Path.

Evidence coverage is NEVER a naive average of raw scores (see the product's explicit "don't just
average every score" rule). Per skill, evidence_engine's confidence (0-1, itself already a
function of source breadth + score strength) is weighted by the skill's role importance
(skill_graph's weight) and averaged -- this measures "how much of what this role needs is
well-evidenced", which is a meaningfully different (and more honest) number than averaging
whatever raw test scores happen to exist. measured_score, confidence, and state stay separate,
visible fields per skill so "why this %?" is always answerable from real data, never a black
box.

This is the canonical career-level EVIDENCE COVERAGE number: how much of what the target role
needs is backed by real evidence. Phase 2G renamed it from readiness_percent, because it is not
readiness and never was. The product computes several genuinely different figures, each scoped
to its own question, and each keeps its own name:

  career_path/service._compute_job_readiness   genuine READINESS -- a five-signal average
  job_hunt/readiness.compute                   apply readiness, "can I apply to this today"
  career_path summary preparation_percent      PREPARATION -- checklist items completed
  progress_service                             PERFORMANCE -- a mean of recent test scores

Anything new that wants to say how well-evidenced a student is for their target role belongs
here and must not introduce a competing number -- two screens disagreeing about one student is
how a figure stops being believed at all.
"""

import datetime
from typing import Dict, List, Mapping, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.profile import StudentProfile
from app.models.readiness_snapshot import ReadinessSnapshot
from app.schemas.career_twin import CareerTwinRead, SkillTwinItemRead
from app.services import application_outcome, evidence_engine, notification_service
from app.services.role_context import resolve_role_context
from app.services.weakness_service import compute_weaknesses

_TREND_LOOKBACK_MIN_DAYS = 6  # "this week" -- see get_current's trend section
# ...and the other end of that window. Without an upper bound, "+N% this week" was computed
# against the newest snapshot that was *at least* 6 days old -- which, for a student who used
# the app in March and came back in September, meant a six-month delta presented as a week's
# progress. A baseline older than this is not "this week" by any reading, so no trend is
# reported at all rather than an impressive-looking but false one.
_TREND_LOOKBACK_MAX_DAYS = 14
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
    db: Session, profile_id: int, role_key: str, today: datetime.date,
    evidence_coverage_percent: int, per_skill: Dict[str, int]
) -> Optional[int]:
    """Returns the evidence_coverage_percent on record for this profile+role immediately BEFORE
    this write -- today's own prior value if this is a second read the same day, else the most
    recent earlier day's value, else None on a genuine first-ever read. This is the correct
    baseline for "did coverage just cross a milestone": comparing against it (rather than
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
        baseline = existing.evidence_coverage_percent
        existing.evidence_coverage_percent = evidence_coverage_percent
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
    baseline = prior.evidence_coverage_percent if prior is not None else None
    db.add(
        ReadinessSnapshot(
            profile_id=profile_id,
            role_family_key=role_key,
            snapshot_date=today,
            evidence_coverage_percent=evidence_coverage_percent,
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
        winner.evidence_coverage_percent = evidence_coverage_percent
        winner.per_skill_confidence_percent = per_skill
        db.commit()
    return baseline


def _compute_trend(db: Session, profile_id: int, role_key: str, today: datetime.date, current_percent: int) -> Optional[int]:
    newest_allowed = today - datetime.timedelta(days=_TREND_LOOKBACK_MIN_DAYS)
    oldest_allowed = today - datetime.timedelta(days=_TREND_LOOKBACK_MAX_DAYS)
    prior = (
        db.query(ReadinessSnapshot)
        .filter(
            ReadinessSnapshot.profile_id == profile_id,
            ReadinessSnapshot.role_family_key == role_key,
            ReadinessSnapshot.snapshot_date <= newest_allowed,
            ReadinessSnapshot.snapshot_date >= oldest_allowed,
        )
        .order_by(ReadinessSnapshot.snapshot_date.desc())
        .first()
    )
    if prior is None:
        return None
    return current_percent - prior.evidence_coverage_percent


def _evidence_for(
    db: Session,
    profile_id: int,
    skills: List[str],
    cached: Optional[Mapping[str, evidence_engine.SkillEvidenceView]],
) -> List[evidence_engine.SkillEvidenceView]:
    """Evidence for `skills`, reusing anything the caller already fetched.

    A cache with a fallback, never a substitute. Career Strategy runs an evidence pass to build
    its signal bundle and then called this service, which ran the same pass again -- the cost
    was documented and accepted because the alternative looked like computing coverage in the
    strategy layer, which would have been a second evidence engine.

    Handing the views over instead keeps this service the sole owner of coverage while removing
    the duplicate read. Any skill the caller did not bring is still fetched here, so a caller
    whose skill set differs from this role's cannot quietly change the answer -- it can only
    make it cheaper. Order follows `skills` so the cached and uncached paths return identically
    ordered results.
    """
    if cached is None:
        return evidence_engine.get_all_skill_evidence(db, profile_id, skills)

    absent = [skill for skill in skills if skill not in cached]
    fetched = {
        view.skill: view
        for view in (evidence_engine.get_all_skill_evidence(db, profile_id, absent) if absent else [])
    }
    views: List[evidence_engine.SkillEvidenceView] = []
    for skill in skills:
        view = cached.get(skill) or fetched.get(skill)
        if view is not None:
            views.append(view)
    return views


def get_current(
    db: Session,
    profile: StudentProfile,
    *,
    evidence: Optional[Mapping[str, evidence_engine.SkillEvidenceView]] = None,
) -> CareerTwinRead:
    """The student's Career Twin.

    `evidence` is an optional cache of already-fetched evidence views (see `_evidence_for`). It
    changes nothing about the result -- only how many times the database is asked for the same
    rows. Evidence coverage is still computed here and nowhere else.
    """
    # Counted BEFORE the two early returns below, and that placement is the whole point: what a
    # student has applied to has nothing to do with whether they have picked a target role.
    # Computing it only on the path that produces a coverage number meant a student with no
    # role -- or a role with no defined skills -- was told they had 0 offers while holding one.
    # A count that reports zero when the truth is one is exactly the quiet falsehood this
    # feature exists to avoid, so it is gathered once here and reported on every path.
    applications = application_outcome.context_for_profile(db, profile.id)

    context = resolve_role_context(db, profile)
    if context is None:
        return CareerTwinRead(
            has_role=False,
            role_source="none",
            reasons=["Select a target role to build your Career Twin."],
            **_application_fields(applications),
        )

    if not context.weights:
        return CareerTwinRead(
            has_role=True,
            role_family_key=context.role_family_key,
            display_role_name=context.display_role_name,
            role_source=context.role_source,
            is_estimate=context.is_estimate,
            reasons=["No required skills are defined for this role yet."],
            **_application_fields(applications),
        )

    skills = list(context.weights.keys())
    evidence_views = _evidence_for(db, profile.id, skills, evidence)

    skill_items = [
        SkillTwinItemRead(
            skill=view.skill,
            weight=context.weights[view.skill],
            measured_score=_measured_score(view.items),
            confidence_percent=round(view.confidence * 100),
            confidence_band=view.confidence_band,
            state=view.state,
            last_verified_at=view.last_verified_at,
        )
        for view in evidence_views
    ]

    total_weight = sum(context.weights.values())
    # None, not 0, when there is nothing to weigh. Zero weight means the role defines no skills
    # to measure against -- we cannot compute the number at all, which is a different statement
    # from "we measured you and found nothing". The old `else 0` published the second, and the
    # Android screen's own "Not enough data yet" branch could never fire because the backend
    # always sent a confident-looking 0.
    #
    # The formula itself is untouched -- Phase 2G renamed this metric without touching a
    # single term of it. EVIDENCE COVERAGE: the weighted mean of evidence confidence across
    # the role's skills, which is what this always measured.
    evidence_coverage_percent = (
        round(100 * sum(context.weights[s.skill] * (s.confidence_percent / 100) for s in skill_items) / total_weight)
        if total_weight
        else None
    )

    today = datetime.date.today()
    per_skill_confidence = {s.skill: s.confidence_percent for s in skill_items}
    # Nothing to record, compare or announce when there is no number. A snapshot is a record of
    # a measurement, and the snapshot column is NOT NULL precisely because a
    # row there asserts one was taken -- writing the unmeasurable case would either raise or,
    # worse, store a 0 that a later trend would read back as a real past score and report as
    # improvement the student never made. The trend and the milestone notification are skipped
    # for the same reason: both are comparisons, and there is nothing here to compare.
    trend = None
    if evidence_coverage_percent is not None:
        baseline_percent = _upsert_snapshot(
            db, profile.id, context.role_family_key, today, evidence_coverage_percent, per_skill_confidence
        )
        trend = _compute_trend(db, profile.id, context.role_family_key, today, evidence_coverage_percent)

        notification_service.commit_career_twin_signals(
            db,
            profile.id,
            context.display_role_name,
            baseline_percent,
            evidence_coverage_percent,
            {s.skill: s.state for s in skill_items},
        )

    biggest_gap = _pick_biggest_gap(skill_items, context.weights)
    next_action_hint = None
    if biggest_gap is not None:
        gap_item = next(s for s in skill_items if s.skill == biggest_gap)
        next_action_hint = evidence_engine.next_action_hint(biggest_gap, gap_item.state)

    weaknesses = compute_weaknesses(db, profile.id, jd_id=None)

    # Which sources this number could not see. Coverage is unchanged by it -- a source we
    # could not read contributes nothing either way -- but a student looking at a partial
    # measurement is entitled to know which part is missing, rather than reading "no evidence"
    # as a finding about them. Computed only on the path that actually produces a number: the
    # no-role and no-skills branches above have nothing for it to qualify.
    missing_sources = evidence_engine.unavailable_sources(db, profile.id)

    return CareerTwinRead(
        has_role=True,
        role_family_key=context.role_family_key,
        display_role_name=context.display_role_name,
        role_source=context.role_source,
        evidence_coverage_percent=evidence_coverage_percent,
        evidence_coverage_trend_percent=trend,
        is_estimate=context.is_estimate,
        reasons=_build_reasons(context.display_role_name, skill_items),
        skills=skill_items,
        biggest_gap_skill=biggest_gap,
        next_action_hint=next_action_hint,
        weak_skill_tags=[w.skill_tag for w in weaknesses[:_MAX_WEAK_TAGS]],
        unavailable_sources=missing_sources,
        **_application_fields(applications),
    )


def _application_fields(context) -> Dict[str, int]:
    """The outcome counts, in one place so all three return paths above report identically.

    Spelled as a helper rather than repeated three times because the failure it prevents is
    silent: a path that forgets one field reports zero for it, and zero is a number a student
    will believe. Reported alongside evidence coverage and absent from its formula -- see
    CareerTwinRead's own note, and application_outcome's module docstring, for why a rejection
    must not move a number that claims to describe the candidate's evidence.
    """
    return {
        "applications_total": context.total,
        "applications_live": context.live,
        "applications_at_interview_stage": context.at_interview_stage,
        "application_offers": context.offers,
        "application_rejections": context.rejections,
    }
