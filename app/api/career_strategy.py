"""Career Strategy endpoints.

Entitlement is enforced here in two different ways, on purpose.

`GET /career-strategy` is available to everyone and *projects* its response: a free student
gets their state, their role, their readiness indicator, the name of their bottleneck and one
recommended action, and the gated sections are simply not in the payload. The screen is useful
without Premium, which is the point -- a paywall over the whole diagnosis would leave a free
student worse informed than before the feature existed.

The other three endpoints are Premium in their entirety and return 403 to a free caller. They
have no meaningful free half: a strategy history with the strategies removed, or a job decision
without the reasoning behind it, would be a worse product than no endpoint at all.

Either way the decision is the server's. `PaywallGate` on Android hides UI; it cannot stop an
authenticated free client calling these routes directly, so nothing here relies on it.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_premium
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.career_strategy import (
    CareerStrategyRead,
    JobStrategyRead,
    SetStrategyTargetsRequest,
    StrategyComparisonRead,
    StrategyHistoryRead,
    TargetComparisonRead,
)
from app.services.career_strategy import service as career_strategy_service
from app.services.career_strategy import snapshots, targets
from app.services.job_hunt import service as job_hunt_service
from app.services.profile_service import get_or_create_profile
from app.services.subscription_service import is_premium

router = APIRouter(prefix="/career-strategy", tags=["career-strategy"])


@router.get("", response_model=CareerStrategyRead)
@limiter.limit("120/hour")
def get_career_strategy(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerStrategyRead:
    """The strategy screen: state, bottleneck, and -- for Premium -- the working behind it.

    Rate limited because one call runs the whole signal pipeline. The limit is generous enough
    that no ordinary use of the app will reach it, and low enough that a client with a broken
    refresh loop cannot turn the screen into a self-inflicted load test.

    Reading this can write a snapshot, but only when the strategic decision has actually
    changed since the last one recorded (see career_strategy/snapshots.py). Opening the screen
    ten times produces one row, not ten.
    """
    profile = get_or_create_profile(db, current_user)
    return career_strategy_service.get_strategy(
        db, profile, is_premium=is_premium(db, profile.id)
    )


@router.get("/history", response_model=StrategyHistoryRead)
def get_strategy_history(
    limit: int = Query(default=snapshots.DEFAULT_HISTORY_LIMIT, ge=1, le=snapshots.MAX_HISTORY_LIMIT),
    current_user: User = Depends(require_premium),
    db: Session = Depends(get_db),
) -> StrategyHistoryRead:
    """Premium: every point at which this student's strategy actually moved, newest first."""
    profile = get_or_create_profile(db, current_user)
    return career_strategy_service.get_history(db, profile.id, limit)


@router.get("/compare/{snapshot_id}", response_model=StrategyComparisonRead)
def compare_snapshot(
    snapshot_id: int,
    current_user: User = Depends(require_premium),
    db: Session = Depends(get_db),
) -> StrategyComparisonRead:
    """Premium: what changed between one recorded strategy and the one before it.

    A snapshot belonging to another student is a 404, never a 403 -- see
    career_strategy_service.SnapshotNotFoundError for why the two cases are not distinguished.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        return career_strategy_service.compare(db, profile.id, snapshot_id)
    except career_strategy_service.SnapshotNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/job/{listing_id}", response_model=JobStrategyRead)
@limiter.limit("120/hour")
def get_job_strategy(
    request: Request,
    listing_id: int,
    current_user: User = Depends(require_premium),
    db: Session = Depends(get_db),
) -> JobStrategyRead:
    """Premium: apply now, prepare first, or not enough information -- for one advert.

    Free students keep the match score and the skill gaps on the Job Hunt screen itself; what
    Premium adds here is the decision and the reasoning.
    """
    profile = get_or_create_profile(db, current_user)
    try:
        return career_strategy_service.get_job_strategy(db, profile, listing_id)
    except job_hunt_service.ListingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/targets", response_model=TargetComparisonRead)
@limiter.limit("60/hour")
def get_strategy_targets(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TargetComparisonRead:
    """Premium: the same strategy engine run against each of the student's target roles.

    Not behind `require_premium`, deliberately. A free client still needs to render this screen
    in its locked state, and a 403 gives it nothing to render -- so the response comes back with
    `locked=true` and genuinely no comparison data in it. The gating is the same either way: the
    server decides, and the payload a free client receives contains nothing it has not paid for.

    Rate limited more tightly than the main screen: each target costs its own signal pass.
    """
    profile = get_or_create_profile(db, current_user)
    return career_strategy_service.compare_targets(
        db, profile, is_premium=is_premium(db, profile.id)
    )


@router.post("/targets", response_model=TargetComparisonRead)
@limiter.limit("60/hour")
def set_strategy_targets(
    request: Request,
    payload: SetStrategyTargetsRequest,
    current_user: User = Depends(require_premium),
    db: Session = Depends(get_db),
) -> TargetComparisonRead:
    """Premium: replace the set of roles compared against.

    Replace rather than append, so the client never has to diff its own state against the
    server's, and an empty list is a real request -- "stop comparing" has to be expressible.

    422 for a role that does not exist or has no skill weights: a target with nothing to measure
    against would otherwise produce a confident-looking strategy with no gaps in it, which reads
    as "you are ready" rather than "this role has no data".
    """
    profile = get_or_create_profile(db, current_user)
    try:
        targets.set_targets(db, profile.id, list(payload.role_ids))
    except (targets.InvalidTargetError, targets.NoRoleWeightsError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return career_strategy_service.compare_targets(db, profile, is_premium=True)
