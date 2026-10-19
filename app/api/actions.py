"""Safe app actions (VA-7): preview, confirm, execute, verify.

Two endpoints, in that order, and the order is the security model. `/actions/preview` resolves
what would happen and mints a single-use confirmation bound to it. `/actions/execute` spends
that confirmation, re-checks everything, runs the mutation through the existing operation-claim
guard and reports a state that was read back from the database rather than inferred from the
fact that the handler returned.

**Why a second surface at all, when `POST /job-hunt/saved` already saves a job.** It is not a
second implementation: both endpoints call the same `job_hunt.service.save_job`. What this adds
is the part the existing route does not need and an assistant-driven call cannot do without --
a preview the student sees, a confirmation bound to exactly that preview, and a verified
outcome. The screen-driven route stays untouched; a student tapping Save in the tracker has
already confirmed by tapping, and asking them twice would be worse, not safer.

**What the model may and may not do.** It may name a capability, which is validated against the
registry, and supply arguments, which are validated against that capability's schema with
unknown fields rejected. It cannot supply identity -- `ToolContext.authenticate` derives the
user from the bearer token -- and it cannot supply a confirmation, because the only confirmation
the server accepts is one it minted and handed to the student's own client.

Every failure before execution is an HTTP error. Every path that reached execution answers 200
with a state, because once a mutation has been attempted "succeeded", "failed", "verification
failed" and "unknown" are all real answers and flattening them into status codes would lose the
only one that matters most: unknown.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.core.rate_limit import limiter
from app.models.user import User
from app.services import idempotency
from app.tools.contracts import (
    ActionState,
    ProfileNotReady,
    ToolCancelled,
    ToolConfirmationInvalid,
    ToolConfirmationRequired,
    ToolContext,
    ToolError,
    ToolExecutionFailed,
    ToolIdempotencyKeyRequired,
    ToolInputInvalid,
    ToolNotFound,
    ToolPermissionDenied,
    ToolResourceNotFound,
    ToolTimeout,
)
from app.tools.registry import build_default_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/actions", tags=["actions"])

#: Built once. `include_actions=True` is the only place in the application that asks for the
#: write catalogue, which is what makes "who can reach a mutation tool" a one-line answer.
#: `include_world=False` because an action never needs an outbound provider call, and a
#: catalogue that cannot make one cannot spend quota on this path.
_REGISTRY = build_default_registry(include_world=False, include_actions=True)


class ActionRequest(BaseModel):
    """What the client asks for. `extra="forbid"` so an invented top-level field -- a
    `confirmed: true`, a `profile_id` -- is a 422 rather than something quietly ignored."""

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=60)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ActionExecuteRequest(ActionRequest):
    confirmation_token: str = Field(min_length=1, max_length=128)


class ActionPreviewResponse(BaseModel):
    state: str
    capability: str
    confirmation_id: str
    #: Returned exactly once, in the response to the request that minted it. Never logged, never
    #: stored in plaintext, never re-readable.
    confirmation_token: str
    description: str
    resource: str
    risk_class: str
    expires_in_seconds: float
    already_satisfied: bool
    requires_idempotency_key: bool


class ActionOutcomeResponse(BaseModel):
    state: str
    capability: str
    verified: bool
    description: str
    resource: str
    detail: Optional[str] = None
    no_change_needed: bool
    replayed: bool


def _context(current_user: User, db: Session) -> ToolContext:
    """The tool context for an already-authenticated HTTP caller.

    `get_current_user` has resolved the bearer token through the same
    `authenticate_access_token` that `ToolContext.authenticate` uses, so re-parsing the header
    here would be a second authentication of one request -- and the second one is the one that
    could drift. The user object is the output of that check, not an identifier supplied by the
    caller.
    """
    return ToolContext(user=current_user, db=db)


def _http_error(exc: ToolError, capability: str) -> HTTPException:
    """One place where a tool failure becomes a status code.

    The codes are chosen so a client can branch without reading message text, which is the same
    contract app/api/ai_errors.py and app/api/idempotency_errors.py already established.

    428 for a missing confirmation is the literal meaning of Precondition Required and is a
    normal step in the flow, not an error the student caused. 409 for an invalid one, because
    the recovery is identical to every other conflict in this app: look again and redo it.
    """
    if isinstance(exc, ToolNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ToolResourceNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.safe_message())
    if isinstance(exc, ToolPermissionDenied):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.safe_message())
    if isinstance(exc, ToolInputInvalid):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.safe_message())
    if isinstance(exc, ToolIdempotencyKeyRequired):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message())
    if isinstance(exc, ToolConfirmationRequired):
        return HTTPException(status_code=428, detail=exc.safe_message())
    if isinstance(exc, ToolConfirmationInvalid):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message())
    if isinstance(exc, ProfileNotReady):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message())
    if isinstance(exc, ToolTimeout):
        return HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="That took too long. Please try again.")
    if isinstance(exc, ToolCancelled):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message())
    if isinstance(exc, ToolExecutionFailed):
        # safe_message(), not str(exc): the underlying text can carry a SQL statement.
        logger.warning("action %s failed: %s", capability, type(exc.cause).__name__)
        return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.safe_message())
    logger.warning("unmapped tool error on %s: %s", capability, exc.code)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message())


#: Both action endpoints, per account per hour. The same policy `/coach/ask` and every interview
#: route already carry, so the write boundary is no longer the one surface without a limit --
#: VA-12's D4, where 150 consecutive previews returned 200 with zero 429s.
#:
#: Why preview needs it as much as execute: execute is already bounded by a single-use
#: confirmation, so it cannot multiply mutations. Preview is the amplifier -- authenticated,
#: previously unlimited, and four SQL statements per call. The per-account confirmation quota
#: keeps memory flat, but nothing kept the database quiet.
_RATE = "60/hour"


@router.post("/preview", response_model=ActionPreviewResponse)
@limiter.limit(_RATE)
def preview_action(
    request: Request,  # noqa: ARG001 - required by the rate limiter's signature inspection
    payload: ActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ActionPreviewResponse:
    """Resolve an action against live state and mint a confirmation for exactly that.

    Writes nothing. The capability's own ownership check runs here, so a job that is not this
    student's is refused at this step with the same undisclosing 404 the execute step gives --
    a preview that leaked existence would make this endpoint the probe.
    """
    context = _context(current_user, db)
    try:
        token, grant, preview = _REGISTRY.preview_action(payload.capability, payload.arguments, context)
    except ToolError as exc:
        raise _http_error(exc, payload.capability)

    spec = _REGISTRY.get(payload.capability)
    return ActionPreviewResponse(
        state=ActionState.CONFIRMATION_REQUIRED.value,
        capability=grant.capability,
        confirmation_id=grant.confirmation_id,
        confirmation_token=token,
        description=preview.description,
        resource=preview.resource_label,
        risk_class=grant.risk_class.value,
        expires_in_seconds=round(grant.expires_in_seconds(), 1),
        already_satisfied=preview.already_satisfied,
        requires_idempotency_key=spec.idempotency.value == "key_required",
    )


@router.post("/execute", response_model=ActionOutcomeResponse)
@limiter.limit(_RATE)
def execute_action(
    request: Request,  # noqa: ARG001 - required by the rate limiter's signature inspection
    payload: ActionExecuteRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ActionOutcomeResponse:
    """Spend one confirmation and report what the database says happened.

    The confirmation arrives in the body rather than a header because it is part of what this
    request *is*, not metadata about it -- and because a body field cannot be picked up from a
    proxy log or a browser's network pane as readily as a header can be copied between requests.
    """
    context = _context(current_user, db)
    try:
        outcome = _REGISTRY.invoke_action(
            payload.capability,
            payload.arguments,
            context,
            confirmation_token=payload.confirmation_token,
            idempotency_key=idempotency_key,
        )
    except idempotency.IdempotencyError as exc:
        # A key that already names a different request, a malformed key, or a result that no
        # longer exists. The existing mapping, so these read the same as on every other
        # protected route rather than inventing a second vocabulary.
        raise idempotency_http_error(exc, logger, f"tool.{payload.capability}")
    except ToolError as exc:
        raise _http_error(exc, payload.capability)

    return ActionOutcomeResponse(
        state=outcome.state.value,
        capability=outcome.capability,
        verified=outcome.verified,
        description=outcome.description,
        resource=outcome.resource_label,
        detail=outcome.detail,
        no_change_needed=outcome.no_change_needed,
        replayed=outcome.replayed,
    )
