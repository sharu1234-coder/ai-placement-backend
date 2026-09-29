"""Strategy history: what was decided, when it changed, and what changed about it.

Two jobs, kept apart on purpose.

The first is deciding whether anything actually changed. `fingerprint` hashes the *material*
decision -- the state, the primary bottleneck's identity and its bands, the queue in order, the
deferred list -- and nothing else. Free text, timestamps and occurrence counts are excluded, so
a weakness being seen a fourth time does not manufacture a history entry claiming the strategy
moved. That exclusion is the whole reason this module can promise "one row per real change"
rather than "one row per screen open".

The second is explaining a change once one has happened. `explain` diffs two stored decisions
and reports only what the two payloads support. It never speculates about cause: it can say the
bottleneck moved from Python to interview communication, because both snapshots say so; it
cannot say the student's practice caused it, because nothing here records that, and a plausible
story is still a fabrication.

One honesty rule shows up twice below and is worth naming. An item that has left the queue has
not necessarily been dealt with -- the queue is capped, so a fifth-placed item can be pushed out
by something new arriving. When the previous queue was short enough that nothing could have been
displaced, "cleared" is a fact. When it was full, the honest word is "dropped out of your top
priorities", and that is what gets said.

Deterministic. No clock beyond the one passed in, no AI, no network.
"""

import datetime
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.placement_strategy_snapshot import PlacementStrategySnapshot
from app.services.career_strategy.bottleneck import MAX_QUEUE_ITEMS, StrategyDecision

# How many history entries a client can ask for at once. History is a review aid, not an audit
# export, and an unbounded list is a denial-of-service surface on a table the user controls the
# growth of.
MAX_HISTORY_LIMIT = 50
DEFAULT_HISTORY_LIMIT = 20

# One retry is enough: the only way the insert can collide is another request for the same
# profile taking the same sequence number, and after re-reading, that request's row is visible.
_MAX_SEQUENCE_RETRIES = 2


def _material(decision: StrategyDecision) -> Dict[str, Any]:
    """The part of a decision that constitutes *the strategy*, for change detection.

    Deliberately excludes every free-text field. `state_reason`, a bottleneck's `description`
    and `evidence`, and a queue item's `reason` are all rendered from the same underlying
    signals that produced the structure below -- so including them would add no information
    while making the fingerprint sensitive to a copy-edit. A reworded explanation is not a
    change of plan, and history should not claim it is.
    """
    primary = decision.primary
    return {
        "version": decision.strategy_version,
        "state": decision.state,
        "primary": (
            None
            if primary is None
            else {
                "id": primary.id,
                "category": primary.category,
                "subject": primary.subject,
                "severity": primary.severity,
                "impact": primary.impact,
                "effort": primary.effort,
                "confidence": primary.confidence,
                "destination": primary.destination,
            }
        ),
        # Order is part of the decision: the same five items in a different order is different
        # advice about what to do first.
        "queue": [[item.id, item.status, item.impact, item.effort] for item in decision.queue],
        "deferred": [item.id for item in decision.do_not_do_yet],
    }


def fingerprint(decision: StrategyDecision) -> str:
    """A stable hash of the material decision. `sort_keys` keeps it independent of dict order."""
    payload = json.dumps(_material(decision), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ChangedItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    category: str
    subject: str
    title: str


class StrategyChange(BaseModel):
    """What moved between two decisions, stated only from what both payloads contain."""

    model_config = ConfigDict(frozen=True)

    changed: bool
    # Null for the very first snapshot: there is no earlier decision to compare against, and
    # comparing against an assumed empty one would report a student's whole starting position
    # as though they had just acquired every problem on it.
    from_sequence: Optional[int] = None
    to_sequence: Optional[int] = None
    from_created_at: Optional[datetime.datetime] = None
    to_created_at: Optional[datetime.datetime] = None

    state_from: Optional[str] = None
    state_to: Optional[str] = None
    state_changed: bool = False

    bottleneck_changed: bool = False
    bottleneck_from: Optional[ChangedItem] = None
    bottleneck_to: Optional[ChangedItem] = None

    # Left the queue while the queue had room to spare -- nothing could have displaced them.
    cleared: List[ChangedItem] = []
    # Left a full queue. Possibly done, possibly outranked; the payloads cannot tell the two
    # apart, so this list is worded as displacement rather than completion.
    displaced: List[ChangedItem] = []
    added: List[ChangedItem] = []
    still_open: List[ChangedItem] = []

    # True when the two decisions were produced by different engine versions. Any comparison
    # across a bump is partly a comparison of the rules, not only of the student.
    spans_version_change: bool = False
    version_from: Optional[int] = None
    version_to: Optional[int] = None

    summary: List[str] = []


def _item(entry: Dict[str, Any]) -> ChangedItem:
    return ChangedItem(
        id=str(entry.get("id", "")),
        category=str(entry.get("category", "")),
        subject=str(entry.get("subject", "")),
        title=str(entry.get("title", "")),
    )


def _queue_index(payload: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not payload:
        return {}
    return {str(entry.get("id", "")): entry for entry in payload.get("queue") or []}


def _primary(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    primary = payload.get("primary")
    return primary if isinstance(primary, dict) else None


def explain(
    previous: Optional[Dict[str, Any]],
    current: Dict[str, Any],
    *,
    from_sequence: Optional[int] = None,
    to_sequence: Optional[int] = None,
    from_created_at: Optional[datetime.datetime] = None,
    to_created_at: Optional[datetime.datetime] = None,
) -> StrategyChange:
    """Diff two serialised StrategyDecisions.

    Both arguments are the stored JSON payloads rather than model instances, because that is
    what history actually holds: a decision recorded months ago under an older engine may not
    round-trip cleanly into today's model, and a comparison that crashes on old data is worse
    than one that reads it defensively.
    """
    if previous is None:
        # Nothing to diff against. Reporting the whole starting queue as "added" would tell a
        # student they had just acquired every problem on it, when in fact this is simply the
        # first time anything was recorded.
        return StrategyChange(
            changed=False,
            to_sequence=to_sequence,
            to_created_at=to_created_at,
            state_to=current.get("state"),
            version_to=current.get("strategy_version"),
            summary=["This is your first recorded strategy, so there is nothing to compare it to yet."],
        )

    prev_queue = _queue_index(previous)
    curr_queue = _queue_index(current)

    prev_primary = _primary(previous)
    curr_primary = _primary(current)

    prev_state = previous.get("state")
    curr_state = current.get("state")

    version_from = previous.get("strategy_version")
    version_to = current.get("strategy_version")

    gone = [entry for key, entry in prev_queue.items() if key not in curr_queue]
    # The cap is what makes departure ambiguous. A previously-full queue could have pushed an
    # item out; a short one could not have.
    previous_queue_was_full = len(prev_queue) >= MAX_QUEUE_ITEMS
    cleared = [] if previous_queue_was_full else [_item(entry) for entry in gone]
    displaced = [_item(entry) for entry in gone] if previous_queue_was_full else []

    added = [_item(entry) for key, entry in curr_queue.items() if key not in prev_queue]
    still_open = [_item(entry) for key, entry in curr_queue.items() if key in prev_queue]

    state_changed = prev_state != curr_state
    prev_primary_id = None if prev_primary is None else prev_primary.get("id")
    curr_primary_id = None if curr_primary is None else curr_primary.get("id")
    bottleneck_changed = prev_primary_id != curr_primary_id

    spans_version_change = version_from is not None and version_from != version_to

    summary: List[str] = []
    if spans_version_change:
        summary.append(
            f"These two decisions were produced by different versions of the strategy engine "
            f"(v{version_from} and v{version_to}), so part of the difference is a change in how "
            "your data is weighed, not a change in your data."
        )
    if state_changed:
        summary.append(f"Your strategy state moved from {prev_state} to {curr_state}.")
    if bottleneck_changed:
        if prev_primary is None:
            summary.append(f"You now have a clear main focus: {curr_primary.get('title', '')}.")
        elif curr_primary is None:
            summary.append(
                f"There is no single main focus right now. Previously it was "
                f"{prev_primary.get('title', '')}."
            )
        else:
            summary.append(
                f"Your main focus changed from {prev_primary.get('title', '')} to "
                f"{curr_primary.get('title', '')}."
            )
    for entry in cleared:
        summary.append(f"Cleared: {entry.title}.")
    for entry in displaced:
        summary.append(
            f"{entry.title} dropped out of your top priorities. It may be resolved, or "
            "something more urgent may have taken its place."
        )
    for entry in added:
        summary.append(f"New in your queue: {entry.title}.")
    if not summary:
        summary.append("Nothing about your strategy has changed since the last snapshot.")

    return StrategyChange(
        changed=bool(state_changed or bottleneck_changed or cleared or displaced or added),
        from_sequence=from_sequence,
        to_sequence=to_sequence,
        from_created_at=from_created_at,
        to_created_at=to_created_at,
        state_from=prev_state,
        state_to=curr_state,
        state_changed=state_changed,
        bottleneck_changed=bottleneck_changed,
        bottleneck_from=None if prev_primary is None else _item(prev_primary),
        bottleneck_to=None if curr_primary is None else _item(curr_primary),
        cleared=cleared,
        displaced=displaced,
        added=added,
        still_open=still_open,
        spans_version_change=spans_version_change,
        version_from=version_from,
        version_to=version_to,
        summary=summary,
    )


# -- persistence ------------------------------------------------------------------------------


def latest(db: Session, profile_id: int) -> Optional[PlacementStrategySnapshot]:
    return (
        db.query(PlacementStrategySnapshot)
        .filter(PlacementStrategySnapshot.profile_id == profile_id)
        .order_by(PlacementStrategySnapshot.sequence.desc())
        .first()
    )


def history(
    db: Session, profile_id: int, limit: int = DEFAULT_HISTORY_LIMIT
) -> List[PlacementStrategySnapshot]:
    """Newest first, capped. See MAX_HISTORY_LIMIT."""
    bounded = max(1, min(limit, MAX_HISTORY_LIMIT))
    return (
        db.query(PlacementStrategySnapshot)
        .filter(PlacementStrategySnapshot.profile_id == profile_id)
        .order_by(PlacementStrategySnapshot.sequence.desc())
        .limit(bounded)
        .all()
    )


def history_with_baseline(
    db: Session, profile_id: int, limit: int = DEFAULT_HISTORY_LIMIT
) -> Tuple[List[PlacementStrategySnapshot], Optional[PlacementStrategySnapshot]]:
    """The same page, plus the one row immediately older than it.

    That extra row is what lets every entry on the page -- including the oldest -- be described
    in terms of what changed, from a single query. Without it the client would have to call
    /compare once per history row, which is a client-side N+1 over a list the server already
    holds in memory.
    """
    bounded = max(1, min(limit, MAX_HISTORY_LIMIT))
    rows = (
        db.query(PlacementStrategySnapshot)
        .filter(PlacementStrategySnapshot.profile_id == profile_id)
        .order_by(PlacementStrategySnapshot.sequence.desc())
        .limit(bounded + 1)
        .all()
    )
    if len(rows) > bounded:
        return rows[:bounded], rows[bounded]
    return rows, None


def get(db: Session, profile_id: int, snapshot_id: int) -> Optional[PlacementStrategySnapshot]:
    """Scoped by profile, always. A bare primary-key lookup here would be an IDOR: snapshot ids
    are sequential integers and a student's strategy is the most personal thing this app holds.
    """
    return (
        db.query(PlacementStrategySnapshot)
        .filter(
            PlacementStrategySnapshot.profile_id == profile_id,
            PlacementStrategySnapshot.id == snapshot_id,
        )
        .first()
    )


def previous_of(
    db: Session, profile_id: int, snapshot: PlacementStrategySnapshot
) -> Optional[PlacementStrategySnapshot]:
    return (
        db.query(PlacementStrategySnapshot)
        .filter(
            PlacementStrategySnapshot.profile_id == profile_id,
            PlacementStrategySnapshot.sequence < snapshot.sequence,
        )
        .order_by(PlacementStrategySnapshot.sequence.desc())
        .first()
    )


def _insert(
    db: Session,
    profile_id: int,
    decision: StrategyDecision,
    digest: str,
    sequence: int,
    now: datetime.datetime,
) -> PlacementStrategySnapshot:
    primary = decision.primary
    row = PlacementStrategySnapshot(
        profile_id=profile_id,
        sequence=sequence,
        strategy_version=decision.strategy_version,
        state=decision.state,
        primary_bottleneck_id=None if primary is None else primary.id,
        primary_category=None if primary is None else primary.category,
        primary_subject=None if primary is None else primary.subject,
        decision_fingerprint=digest,
        decision=decision.model_dump(mode="json"),
        created_at=now,
    )
    db.add(row)
    return row


def record(
    db: Session,
    profile_id: int,
    decision: StrategyDecision,
    *,
    now: Optional[datetime.datetime] = None,
) -> Tuple[PlacementStrategySnapshot, bool]:
    """Persist this decision only if it differs from the last one recorded.

    Returns (snapshot, created). `created` is False when the existing latest row already
    represents this decision -- which is the common case, because the screen is opened far more
    often than the strategy moves.

    The insert runs inside a savepoint. Two concurrent reads for the same profile can both
    compute the same next sequence number, and the unique constraint turns the loser into an
    IntegrityError. `db.begin_nested()` is what lets that be recovered from without discarding
    the caller's surrounding transaction -- a bare `db.rollback()` here would silently undo
    whatever else the request had already done.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    digest = fingerprint(decision)

    for _attempt in range(_MAX_SEQUENCE_RETRIES):
        current = latest(db, profile_id)
        if current is not None and current.decision_fingerprint == digest:
            return current, False

        next_sequence = 1 if current is None else current.sequence + 1
        try:
            with db.begin_nested():
                row = _insert(db, profile_id, decision, digest, next_sequence, now)
            db.commit()
            return row, True
        except IntegrityError:
            # Someone else took this sequence number. Releasing the savepoint is what raised,
            # and the nested transaction has already rolled back to it, so the surrounding
            # transaction is still intact -- which is the entire reason for the savepoint
            # rather than a bare db.rollback() here. Expire the identity map so the re-read at
            # the top of the loop sees the winner's row and not our stale view of the table.
            db.expire_all()

    # Both attempts collided, which means a concurrent writer is present and has committed
    # something. Return whatever is now current rather than raising: the caller asked for a
    # strategy, and failing their read because a second request wrote first would be a worse
    # outcome than handing back the row that request produced.
    current = latest(db, profile_id)
    if current is not None:
        return current, False
    raise RuntimeError("Unable to record a strategy snapshot")
