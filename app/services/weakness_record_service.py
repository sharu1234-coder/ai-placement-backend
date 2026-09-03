"""Interview Memory -- turns weakness_service's live, stateless per-interview weakness view
into cross-interview memory for interview evaluation dimensions specifically. Every dimension
of every completed interview evaluation calls record_or_update once (see the hook in
interview_service.get_evaluation); a WeaknessRecord row is created only the first time a
dimension is ever weak, then updated on every subsequent evaluation, weak or not.

Status machine, deliberately graduated rather than binary (the spec's own callout: "one
correct answer is not enough" to call a weakness resolved):
  NEW            -- flagged weak for the first time.
  REPEATED       -- flagged weak again (whether or not it had started improving).
  IMPROVING      -- 1 consecutive good signal after being weak.
  NEARLY_CLOSED  -- 2 consecutive good signals.
  CLOSED         -- 3 consecutive good signals -- the only status that counts as resolved.
A single weak signal at any point (including from CLOSED) immediately resets the streak to 0
and moves status back to REPEATED -- regression is never silently absorbed into "still
improving."
"""

import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.weakness_record import WeaknessRecord
from app.services import notification_service
from app.services.weakness_service import INTERVIEW_DIMENSION_THRESHOLD

# The two statuses worth a notification -- turning the corner (IMPROVING) and full resolution
# (CLOSED). NEARLY_CLOSED is a real intermediate step but not notified on its own, so a student
# isn't pinged at every single incremental signal.
_NOTABLE_STATUSES = {"IMPROVING", "CLOSED"}

_NEARLY_CLOSED_STREAK = 2
_CLOSED_STREAK = 3

# Lower rank = shown first: open/regressed weaknesses before ones already trending toward
# resolution, fully closed ones last.
_STATUS_RANK = {"NEW": 0, "REPEATED": 0, "IMPROVING": 1, "NEARLY_CLOSED": 2, "CLOSED": 3}


def _display_label(skill_key: str) -> str:
    label = skill_key.removeprefix("interview:").removeprefix("recruiter_attack:")
    return label.replace("_", " ").capitalize()


def _get(db: Session, profile_id: int, skill_key: str) -> Optional[WeaknessRecord]:
    return (
        db.query(WeaknessRecord)
        .filter(WeaknessRecord.profile_id == profile_id, WeaknessRecord.skill_key == skill_key)
        .first()
    )


def record_or_update(db: Session, profile_id: int, skill_key: str, score: float) -> Optional[WeaknessRecord]:
    is_weak = score < INTERVIEW_DIMENSION_THRESHOLD
    now = datetime.datetime.now(datetime.timezone.utc)
    existing = _get(db, profile_id, skill_key)

    if existing is None:
        if not is_weak:
            # Never been weak -- nothing worth remembering yet.
            return None
        record = WeaknessRecord(
            profile_id=profile_id,
            skill_key=skill_key,
            status="NEW",
            occurrences=1,
            consecutive_good_signals=0,
            last_score=score,
            last_seen_at=now,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    old_status = existing.status
    existing.last_score = score
    existing.last_seen_at = now
    if is_weak:
        existing.occurrences += 1
        existing.consecutive_good_signals = 0
        existing.status = "REPEATED"
    else:
        existing.consecutive_good_signals += 1
        if existing.status != "CLOSED":
            if existing.consecutive_good_signals >= _CLOSED_STREAK:
                existing.status = "CLOSED"
            elif existing.consecutive_good_signals >= _NEARLY_CLOSED_STREAK:
                existing.status = "NEARLY_CLOSED"
            else:
                existing.status = "IMPROVING"

    # Only a genuine crossing INTO IMPROVING or CLOSED notifies -- never a re-fire for a
    # student who was already at that status (e.g. already IMPROVING, still IMPROVING).
    if existing.status in _NOTABLE_STATUSES and existing.status != old_status:
        label = _display_label(skill_key)
        if existing.status == "CLOSED":
            body = f"You've fully closed out your {label} weakness -- three good signals in a row."
        else:
            body = f"Your {label} weakness is turning around -- keep it up."
        notification_service.enqueue(
            db, profile_id, kind="weakness_improved", title=f"Progress on {label}!", body=body
        )

    db.commit()
    db.refresh(existing)
    return existing


def list_for_profile(db: Session, profile_id: int) -> List[WeaknessRecord]:
    records = db.query(WeaknessRecord).filter(WeaknessRecord.profile_id == profile_id).all()
    return sorted(records, key=lambda r: (_STATUS_RANK[r.status], -r.occurrences, r.skill_key))
