"""Multi-target strategy: the same engine, pointed at a different role.

The whole design of this module is a refusal to build a second strategy engine. A comparison
target is not a new kind of thing with its own scoring -- it is the existing pipeline run with
`signals.collect(role=...)` overridden, which means every guarantee the single-target path has
(deterministic, evidence-led, no fabricated numbers, no hiring probability) holds here for free
and cannot drift, because there is only one implementation to drift from.

Readiness and effort across roles come from `career_decision_service.simulate`, which already
owns per-role readiness and already computes skill overlap. Calling it once with every target
gets all of that in a single pass rather than N, and -- more importantly -- guarantees that the
readiness shown on the comparison screen is the same number Career Decision Lab shows for the
same role. Two screens disagreeing about one role would make both untrustworthy.

**Comparison runs never write history.** Looking at what your strategy *would* be for another
role is not a change to your strategy. Snapshots are for the student's real target only, so
`compute(persist=False)` is used throughout and is not optional.

**Targets are capped.** Each one costs a full signal pass, and beyond a handful the screen stops
being a decision and becomes a spreadsheet. The cap matches Career Decision Lab's so the two
features cannot disagree about how many roles a person can sensibly weigh at once.
"""

import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.career_strategy_target import CareerStrategyTarget
from app.models.profile import StudentProfile
from app.models.role import Role
from app.services import skill_graph
from app.services.role_context import RoleContext, resolve_role_context

# Career Decision Lab allows 2-3 roles in one simulation; this matches it deliberately. Beyond
# three the comparison stops helping someone decide and starts needing its own analysis.
MAX_COMPARISON_TARGETS = 3


class InvalidTargetError(ValueError):
    """A role id that is not in the catalogue, or too many of them."""


class NoRoleWeightsError(ValueError):
    """The role exists but has no skill weights, so there is nothing to measure against.

    Raised rather than silently returning an empty strategy: a role with no weights would
    produce zero gaps and therefore a confident-looking "nothing to do here", which is the
    opposite of the truth.
    """


def role_context_for(db: Session, role: Role) -> RoleContext:
    """Build the same RoleContext shape role_context.resolve_role_context returns.

    `role_family_key` uses the identical `role:{id}` form that resolve_role_context uses for a
    legacy target role, so a comparison target and the same role chosen as the real target
    produce the same key -- and therefore line up against the same ReadinessSnapshot history
    rather than starting a parallel one.
    """
    weights = skill_graph.get_role_skill_weights_by_role_id(db, role.id)
    if not weights:
        raise NoRoleWeightsError(f"Role {role.name!r} has no skill weights to measure against")
    return RoleContext(
        role_family_key=f"role:{role.id}",
        display_role_name=role.name,
        role_source="target_role",
        is_estimate=False,
        weights=weights,
    )


def list_targets(db: Session, profile_id: int) -> List[CareerStrategyTarget]:
    return (
        db.query(CareerStrategyTarget)
        .filter(CareerStrategyTarget.profile_id == profile_id)
        .order_by(CareerStrategyTarget.position.asc(), CareerStrategyTarget.id.asc())
        .all()
    )


def roles_for(db: Session, profile_id: int) -> List[Role]:
    """The saved targets as Role rows, in the student's chosen order, in one query.

    Deliberately a single `IN` rather than a `db.get` per target: this is small today, and a
    per-row lookup here is exactly the shape that turns into an N+1 the moment the cap is
    raised.
    """
    saved = list_targets(db, profile_id)
    if not saved:
        return []
    by_id: Dict[int, Role] = {
        role.id: role
        for role in db.query(Role).filter(Role.id.in_([row.role_id for row in saved])).all()
    }
    # A role deleted from the catalogue leaves a dangling target; skip it rather than failing
    # the whole comparison, and it simply stops appearing.
    return [by_id[row.role_id] for row in saved if row.role_id in by_id]


def set_targets(
    db: Session, profile_id: int, role_ids: List[int], *, now: Optional[datetime.datetime] = None
) -> List[Role]:
    """Replace the saved comparison set. Returns the resolved roles, in the requested order.

    Validated before anything is written, so a request naming one bad role changes nothing --
    a partial apply would leave the student with a comparison set they never asked for.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)

    # De-duplicate while preserving order: asking for the same role twice is one target, and
    # the request should not be rejected over it.
    ordered: List[int] = []
    for role_id in role_ids:
        if role_id not in ordered:
            ordered.append(role_id)

    if len(ordered) > MAX_COMPARISON_TARGETS:
        raise InvalidTargetError(
            f"You can compare up to {MAX_COMPARISON_TARGETS} target roles at once"
        )

    roles: List[Role] = []
    for role_id in ordered:
        role = db.get(Role, role_id)
        if role is None:
            raise InvalidTargetError(f"Role {role_id} not found")
        # Fail now, before any write, if the role cannot actually be measured against.
        role_context_for(db, role)
        roles.append(role)

    existing = {row.role_id: row for row in list_targets(db, profile_id)}
    for role_id, row in existing.items():
        if role_id not in ordered:
            db.delete(row)

    for position, role_id in enumerate(ordered):
        row = existing.get(role_id)
        if row is not None:
            row.position = position
            continue
        try:
            # A savepoint, not a bare rollback: a concurrent request adding the same target
            # must not discard the deletes and reorders this loop has already applied.
            with db.begin_nested():
                db.add(
                    CareerStrategyTarget(
                        profile_id=profile_id, role_id=role_id, position=position, created_at=now
                    )
                )
        except IntegrityError:
            # Someone else added this exact target first. That is the outcome we wanted, so
            # adopt their row and keep going rather than failing the request.
            db.expire_all()
            claimed = (
                db.query(CareerStrategyTarget)
                .filter(
                    CareerStrategyTarget.profile_id == profile_id,
                    CareerStrategyTarget.role_id == role_id,
                )
                .first()
            )
            if claimed is not None:
                claimed.position = position

    db.commit()
    return roles


def current_target(db: Session, profile: StudentProfile) -> Optional[RoleContext]:
    """The student's real target -- unchanged by anything in this module."""
    return resolve_role_context(db, profile)


def comparison_set(
    db: Session, profile: StudentProfile
) -> Tuple[Optional[RoleContext], List[Tuple[Role, RoleContext]]]:
    """The primary target plus every saved comparison target that can still be measured.

    The primary is returned separately rather than merged into the list because it is not
    interchangeable with the others: it is the one the student's snapshots, history and Career
    Twin readiness actually belong to.
    """
    primary = current_target(db, profile)
    targets: List[Tuple[Role, RoleContext]] = []
    for role in roles_for(db, profile.id):
        try:
            targets.append((role, role_context_for(db, role)))
        except NoRoleWeightsError:
            # The catalogue changed under the saved target. Dropping it is right: there is
            # genuinely nothing to compare, and an empty column would imply there was.
            continue
    return primary, targets
