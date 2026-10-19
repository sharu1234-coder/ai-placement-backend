"""The Voice Career Coach endpoint (VA-8).

One route, `POST /coach/ask`, and it is the provider-neutral integration boundary the rest of
the coaching feature hangs off. It takes text and returns a structured, evidence-partitioned
coaching report. It does not know what produced the text or what will render the answer, which
is the whole point: a future speech path supplies a transcript here and speaks
`CoachingReport`'s sentences back, and neither end of that needs this file to change.

**What this is not.** Not a voice endpoint -- no audio, no streaming, no speech provider. Not a
write path -- it reaches the registry through `invoke_read` only, via the VA-6 orchestrator,
and the catalogue it builds contains no ACTION tool at all. Not a second authorization system
-- identity comes from the bearer token through the same `get_current_user` every other route
uses, and no field in the request body can influence it.

**Why a session id is in the body.** Follow-up turns need the bounded session context VA-6
already implements, and the client is the only party that knows which conversation this turn
belongs to. It is not a credential: `MemoryStore.for_session` checks the authenticated user id
against the one the session was opened with and refuses a mismatch, so a guessed session id
hands back nothing.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agent.contracts import (
    AgentError,
    AgentRequest,
    Intent,
    ModelUnavailable,
    TurnBudget,
    TurnCancelled,
    TurnLimitReached,
    TurnTimeout,
)
from app.agent.memory import store as memory_store
from app.agent.orchestrator import Orchestrator
from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.tools.contracts import ProfileNotReady, ToolContext, ToolError, ToolUnauthenticated
from app.tools.registry import build_default_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/coach", tags=["coach"])

#: Read-only, world included. `include_actions` is left at its default of False, so there is no
#: ACTION tool in this catalogue for the orchestrator to be tempted by -- the read-only
#: guarantee here is a property of the catalogue, not of the orchestrator remembering to use
#: `invoke_read`. (It uses `invoke_read` as well; both, deliberately.)
_REGISTRY = build_default_registry()

#: No model is passed. No production LLM has been selected (VA-3's gate is open) and the cost
#: policy is zero-first, so the coach runs on the deterministic path. When a provider is
#: approved it is injected here and its only job is still picking an `Intent` label.
_ORCHESTRATOR = Orchestrator(_REGISTRY)

#: A coaching turn can fan out to several reads and, when a provider is configured, one
#: outbound call. The TurnBudget bounds a single turn; this bounds how many turns one account
#: can start. Deliberately generous enough for a real conversation.
_RATE = "60/hour"


class CoachAskRequest(BaseModel):
    """`extra="forbid"`, so an invented field -- a `profile_id`, a `user_id`, a `confirmed` --
    is a 422 rather than something silently ignored."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=AgentRequest.MAX_TEXT)
    #: Which conversation this turn continues. Omitted means a fresh, single-turn context.
    session_id: Optional[str] = Field(default=None, max_length=64)
    #: Set by a UI button that already knows what it asked for. Validated against the closed
    #: enum by Pydantic, and an intent still only ever maps to a fixed set of read tools -- so
    #: supplying one chooses a question, never a capability.
    intent_hint: Optional[Intent] = None


class CoachAskResponse(BaseModel):
    """Loosely typed on purpose: the payload is `CoachingTurn.to_payload()`, whose shape is
    owned by `app/agent/coaching.py` and documented there. Restating that schema here would be
    a second definition to keep in step, and the one that drifted would be this one."""

    intent: str
    claims: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    clarifying_question: Optional[str] = None
    failures: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    used_model: bool = False
    coaching: dict[str, Any] = Field(default_factory=dict)


@router.post("/ask", response_model=CoachAskResponse)
@limiter.limit(_RATE)
def ask(
    request: Request,  # noqa: ARG001 - required by the rate limiter's signature inspection
    payload: CoachAskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CoachAskResponse:
    """Answer one coaching turn, grounded in this user's own records.

    Every failure is typed and already redacted by the layer that raised it. Nothing here
    renders an exception's text: a tool failure's message can name a table, and a provider's
    can name a credential.
    """
    context = ToolContext(user=current_user, db=db)

    try:
        memory = memory_store.for_session(payload.session_id or f"turn-{current_user.id}", user_id=current_user.id)
    except PermissionError:
        # A session id belonging to another account. 404, not 403: confirming that the session
        # exists is itself the disclosure.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That conversation was not found.")

    try:
        result = _ORCHESTRATOR.coach(
            AgentRequest(text=payload.text, intent_hint=payload.intent_hint),
            context,
            memory,
            budget=TurnBudget(),
        )
    except ProfileNotReady as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message())
    except ToolUnauthenticated:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired credentials")
    except TurnCancelled as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message())
    except (TurnTimeout, TurnLimitReached) as exc:
        # 504 rather than 500: the request was valid and the server ran out of its own budget,
        # which is a different thing from the request being wrong and is retryable.
        logger.info("coach turn exceeded its budget: %s", exc.code)
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="That took too long. Please try again.")
    except ModelUnavailable:
        # Not reachable today, since no provider is configured and model failures already
        # degrade to the deterministic path inside the orchestrator. Mapped anyway so that
        # approving a provider cannot turn a provider outage into a 500.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Coaching is briefly unavailable.")
    except AgentError as exc:
        logger.warning("coach turn failed: %s", exc.code)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message())
    except ToolError as exc:
        # The orchestrator absorbs per-tool failures into `failures`; anything that escapes is
        # a boundary failure, reported with its safe message only.
        logger.warning("coach turn hit an unhandled tool error: %s", exc.code)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message())

    body = result.to_payload()
    return CoachAskResponse(**body)
