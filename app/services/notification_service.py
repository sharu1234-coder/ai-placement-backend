"""Notifications -- surfaces server-detected state transitions to the student. There is no
backend push-send infrastructure yet (no device-token model, no Firebase Admin integration --
see the Phase 14 scoping decision recorded in the project plan), so this is a local-delivery
design: a transition is enqueued as a NotificationEvent row the moment it's detected (always
inside the same transaction as the write/read that detected it, never a separate commit that
could drift out of sync), and the Android app polls GET /notifications/pending on resume,
posts/records each one locally, then calls the ack endpoint so it is never re-delivered.

Every enqueue call site below is a genuine, already-real state transition -- never a periodic
digest or a guess -- matching the "GOOD" notification examples in the product spec (a real
thing just happened) rather than "Don't forget!" nagging.
"""

import datetime
from typing import Dict, List, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.evidence_state_snapshot import EvidenceStateSnapshot
from app.models.notification_event import NotificationEvent

# Ordered ascending; only a crossing INTO one of these (baseline < milestone <= new) fires,
# and only the single highest one crossed in a jump is reported, so a student who jumps from
# 40% to 90% in one read gets one "95%... wait, 90%" style message, not four stacked ones.
# Thresholds are untouched by Phase 2G: they were chosen for the metric's VALUES, not its
# name, so renaming the metric must not move them. Only the constant's name and the wording
# it drives changed.
_EVIDENCE_COVERAGE_MILESTONES = [50, 70, 85, 95]

# Evidence states that count as "made it" for a notification -- see evidence_engine.py's
# _compute_state_and_confidence for the full claimed/supported/strong/verified/stale scale.
_NOTABLE_EVIDENCE_STATES = {"strong", "verified"}


def enqueue(db: Session, profile_id: int, kind: str, title: str, body: str) -> None:
    """Adds the row but deliberately does not commit -- every call site below fires from
    inside a function that already commits its own write, so the notification rides along in
    that same transaction rather than risking a partial commit if something after this line
    fails."""
    db.add(NotificationEvent(profile_id=profile_id, kind=kind, title=title, body=body))


def get_pending(db: Session, profile_id: int) -> List[NotificationEvent]:
    return (
        db.query(NotificationEvent)
        .filter(NotificationEvent.profile_id == profile_id, NotificationEvent.delivered_at.is_(None))
        .order_by(NotificationEvent.created_at)
        .all()
    )


def acknowledge(db: Session, profile_id: int, ids: List[int]) -> None:
    if not ids:
        return
    db.execute(
        update(NotificationEvent)
        .where(NotificationEvent.profile_id == profile_id, NotificationEvent.id.in_(ids))
        .values(delivered_at=datetime.datetime.now(datetime.timezone.utc))
    )
    db.commit()


def _milestone_crossed(baseline_percent: Optional[int], new_percent: int) -> Optional[int]:
    if baseline_percent is None:
        return None  # No real prior value to compare against -- never fabricate "you improved".
    crossed = [m for m in _EVIDENCE_COVERAGE_MILESTONES if baseline_percent < m <= new_percent]
    return max(crossed) if crossed else None


def check_readiness_milestone(
    db: Session, profile_id: int, display_role_name: str, baseline_percent: Optional[int], new_percent: int
) -> None:
    milestone = _milestone_crossed(baseline_percent, new_percent)
    if milestone is None:
        return
    enqueue(
        db,
        profile_id,
        # The `kind` string stays "readiness_milestone": it is a persisted value already
        # written to notification_events rows, and changing it would split one event type
        # across two names in a student's own history for no gain the student can see.
        # The TEXT is what they read, and the text was claiming readiness for a number that
        # measures evidence coverage -- that is what Phase 2G fixes here.
        kind="readiness_milestone",
        title=f"{milestone}% Evidence Coverage for {display_role_name}!",
        body=f"Your Career Twin evidence coverage just crossed {milestone}% for {display_role_name}.",
    )


def check_evidence_transitions(db: Session, profile_id: int, skill_states: Dict[str, str]) -> None:
    """skill_states: {skill: current evidence_engine state}. Compares each against the last
    state we recorded for that skill and fires only on a genuine crossing INTO strong/verified
    -- a skill already strong/verified on a prior read never re-fires, and a skill that never
    reaches either never does either."""
    if not skill_states:
        return
    existing_rows = {
        row.skill_key: row
        for row in db.query(EvidenceStateSnapshot)
        .filter(EvidenceStateSnapshot.profile_id == profile_id, EvidenceStateSnapshot.skill_key.in_(skill_states.keys()))
        .all()
    }
    now = datetime.datetime.now(datetime.timezone.utc)
    for skill, state in skill_states.items():
        prior = existing_rows.get(skill)
        prior_state = prior.state if prior is not None else None
        if state in _NOTABLE_EVIDENCE_STATES and prior_state not in _NOTABLE_EVIDENCE_STATES:
            enqueue(
                db,
                profile_id,
                kind="evidence_verified",
                title=f"{skill} is now {state.capitalize()}!",
                body=f"Your {skill} evidence just reached {state} status.",
            )
        if prior is None:
            db.add(EvidenceStateSnapshot(profile_id=profile_id, skill_key=skill, state=state, updated_at=now))
        elif prior.state != state:
            prior.state = state
            prior.updated_at = now


def commit_career_twin_signals(
    db: Session,
    profile_id: int,
    display_role_name: str,
    baseline_percent: Optional[int],
    new_percent: int,
    skill_states: Dict[str, str],
) -> None:
    """Run both notification checks for one Career Twin read and commit them, tolerating a
    lost insert race on evidence_state_snapshots.

    check_evidence_transitions inserts a row per never-before-seen skill and deliberately does
    not commit (so its notifications ride in the caller's transaction). That leaves a plain
    read-then-insert against uq_evidence_state_snapshot: two concurrent first-ever reads of
    the same Career Twin -- e.g. the Home screen's GET /career-twin/readiness and GET
    /career-twin firing together on app launch, both of which reach here through
    career_twin_service.get_current -- both see no prior row, both insert, and the loser's
    commit raises an uncaught IntegrityError, 500ing an otherwise successful read.

    A prior pass closed exactly this race for ReadinessSnapshot (career_twin_service's
    _upsert_snapshot) and stopped there, even though this second unique-constrained
    per-(profile, skill) upsert sits three lines further down the same function. Same fix
    shape as that one, hoisted here so both checks are inside it: on a lost race, roll back
    and redo. The redo is safe and self-correcting -- check_evidence_transitions now finds
    the winner's committed rows and takes its update branch, so it neither re-fires a
    notification the winner already enqueued nor re-attempts the conflicting insert, and
    check_readiness_milestone is a pure function of the two percentages passed in, so it
    re-enqueues the milestone the rolled-back transaction dropped.
    """
    def _record() -> None:
        check_readiness_milestone(db, profile_id, display_role_name, baseline_percent, new_percent)
        check_evidence_transitions(db, profile_id, skill_states)

    _record()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _record()
        db.commit()
