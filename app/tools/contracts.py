"""Tool Registry contracts: what a tool must declare, and who it runs as.

Framework-light on purpose. Pydantic is here because the input and output schemas *are* the
contract, and FastAPI's `HTTPException` is here because `authenticate_access_token` raises it
and re-wrapping that would mean a second interpretation of "unauthenticated". Nothing else
from the web layer belongs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from app.models.profile import StudentProfile
from app.models.user import User


class ToolKind(str, Enum):
    """What a tool does to the world.

    The distinction is load-bearing rather than documentation: a READ tool is allowed to run
    without confirmation, and that permission is only safe because READ is *enforced* to mean
    no side effects (see `ToolSpec.__post_init__`). ACTION and NAVIGATION are defined here so
    the classification exists from the start, but VA-4 registers neither -- their
    authorization, confirmation, idempotency and verification design is VA-7's.
    """

    READ = "read"
    ACTION = "action"
    NAVIGATION = "navigation"


class Idempotency(str, Enum):
    NATURAL = "natural"  # repeating it changes nothing: every READ
    KEY_REQUIRED = "key_required"  # caller must supply an Idempotency-Key (VA-7)
    NOT_IDEMPOTENT = "not_idempotent"  # must never be auto-retried


class Verification(str, Enum):
    NONE = "none"
    USER_CONFIRMATION = "user_confirmation"  # VA-7


class RiskClass(str, Enum):
    """How much a mistaken execution would cost the student.

    Declared per action rather than inferred, because the inference would have to be made from
    the handler's body and would be wrong the first time an action grew a second effect. VA-7
    registers `LOW` actions only and applies the strictest confirmation policy to all of them:
    the policy can be relaxed per class later with evidence, and starting strict means a
    relaxation is a visible decision rather than the absence of one.
    """

    LOW = "low"  # reversible by the student, affects only their own tracker
    MEDIUM = "medium"  # reversible with effort, or visible to someone else
    HIGH = "high"  # irreversible, financial, or account-level


class ActionState(str, Enum):
    """Where an action request got to. The lifecycle is the contract.

    `UNKNOWN` is first-class and deliberately not collapsed into `FAILED`. A request that timed
    out or lost its connection mid-mutation has no truthful binary answer, and the dangerous
    reading is "failed", because that invites a retry of something that may already have
    happened. `VERIFICATION_FAILED` is likewise separate from `FAILED`: the mutation was
    attempted and the authoritative read did not show the intended state, which is a different
    problem from the mutation being refused.
    """

    REQUESTED = "requested"
    AUTHORIZED = "authorized"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"
    VERIFICATION_FAILED = "verification_failed"


# --------------------------------------------------------------------------- failures


class ToolError(Exception):
    """Base for every Tool Registry failure.

    Each subclass carries a `code` because the orchestrator that eventually consumes these
    needs to branch on them, and branching on message text is how an error message becomes an
    API contract nobody meant to publish.
    """

    code = "tool_error"

    def safe_message(self) -> str:
        """What a caller -- ultimately a language model -- may be told.

        Deliberately not `str(self)` for the execution-failure case: a database error's text
        can carry table names, column names and fragments of a query. See
        `ToolExecutionFailed`.
        """
        return str(self)


class ToolUnauthenticated(ToolError):
    code = "unauthenticated"


class ToolNotFound(ToolError):
    """An unregistered capability was requested.

    The message names the requested capability but **never** lists the registered ones: a
    model guessing names must not be handed the catalogue as a side effect of guessing wrong.
    """

    code = "unknown_capability"

    def __init__(self, name: str) -> None:
        self.requested = name
        super().__init__(f"no registered capability named '{name}'")


class ToolInputInvalid(ToolError):
    """Arguments failed the tool's schema.

    Carries the field-level summary (field path and error type) but not the submitted values:
    the values are model-generated text derived from a student's own data, and echoing them
    into an error that may be logged or shown is how that data travels somewhere it was never
    meant to go. Same reasoning as `_redact_validation_error` in the Ollama provider.
    """

    code = "invalid_arguments"

    def __init__(self, tool: str, error: ValidationError) -> None:
        self.tool = tool
        self.fields = [
            f"{'.'.join(str(p) for p in item['loc']) or '<root>'} ({item['type']})"
            for item in error.errors()
        ]
        super().__init__(f"{tool} rejected its arguments: {len(self.fields)} problem(s): {', '.join(self.fields)}")


class ToolPermissionDenied(ToolError):
    code = "permission_denied"

    def __init__(self, tool: str, reason: str) -> None:
        self.tool = tool
        super().__init__(f"{tool} is not available to this account: {reason}")


class ProfileNotReady(ToolError):
    """The user is authenticated but has no profile row yet.

    A distinct, non-fatal state rather than an error to paper over. The HTTP routes call
    `get_or_create_profile`, which **writes** -- acceptable for a request the user initiated,
    not acceptable for a READ tool, because a read that creates a row is not a read. So the
    registry looks the profile up read-only and says so when it is absent.
    """

    code = "profile_not_ready"

    def __init__(self) -> None:
        super().__init__("this account has no student profile yet, so there is nothing to read")


class ToolTimeout(ToolError):
    code = "timeout"

    def __init__(self, tool: str, elapsed: float, budget: float) -> None:
        self.tool = tool
        super().__init__(f"{tool} exceeded its {budget:.3g}s budget after {elapsed:.3g}s")


class ToolCancelled(ToolError):
    code = "cancelled"

    def __init__(self, tool: str, stage: str) -> None:
        self.tool = tool
        self.stage = stage
        super().__init__(f"{tool} was cancelled while {stage}")


class ToolResourceNotFound(ToolError):
    """The thing an action was pointed at does not exist, or does not belong to this caller.

    One error for both, with a message that names neither the id nor which of the two it was.
    That is the same choice every ownership check in `app/services/job_hunt/service.py` makes:
    answering "it exists but is not yours" lets a caller enumerate other students' rows by
    watching which ids come back differently.
    """

    code = "resource_not_found"

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"{tool} could not find the item you asked about")


class ToolConfirmationRequired(ToolError):
    """An ACTION tool was asked to run with no confirmation at all.

    Separate from `ToolConfirmationInvalid` so the client can tell "you have not previewed this
    yet" from "the confirmation you sent does not match". The first is a normal step in the
    flow; the second is either a stale screen or an attempt.
    """

    code = "confirmation_required"

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"{tool} requires an explicit user confirmation before it can run")


class ToolConfirmationInvalid(ToolError):
    """The confirmation did not bind to this request.

    One message for every cause -- unknown, expired, already used, different user, different
    capability, different arguments. Naming the cause would turn this into an oracle for
    guessing a token's shape or probing whose confirmation an id belongs to, and the client's
    recovery is the same in every case: preview again.
    """

    code = "confirmation_invalid"

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"the confirmation for {tool} is not valid for this request; please confirm again")


class ToolIdempotencyKeyRequired(ToolError):
    """An action declaring `KEY_REQUIRED` was asked to run without one.

    Enforced rather than papered over with a server-minted key. A key minted here would be
    unique to this request, so the claim row it creates could never match the duplicate it
    exists to collapse -- the mechanism would still be present and would protect nothing.
    """

    code = "idempotency_key_required"

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"{tool} requires an Idempotency-Key so a duplicate submission cannot run twice")


class ToolExecutionFailed(ToolError):
    """A tool's handler raised.

    The original exception is kept on `cause` for the server's own logging and is deliberately
    **not** in `safe_message()`. A SQLAlchemy error's text routinely contains the failing
    statement; handing that to a model that may read it aloud, or echo it into a prompt, would
    turn an internal fault into a schema disclosure.
    """

    code = "execution_failed"

    def __init__(self, tool: str, cause: BaseException) -> None:
        self.tool = tool
        self.cause = cause
        super().__init__(f"{tool} failed: {type(cause).__name__}: {cause}")

    def safe_message(self) -> str:
        return f"{self.tool} could not be completed"


# --------------------------------------------------------------------------- execution context


class CancellationToken:
    """Cooperative cancellation, checked at dispatch boundaries.

    Honest about its granularity, like the speech layer's: a synchronous query cannot be
    interrupted from Python mid-statement. What this guarantees is that a cancelled call does
    not *start*, and that a cancelled multi-step handler stops between steps.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self, tool: str, stage: str) -> None:
        if self._cancelled:
            raise ToolCancelled(tool, stage)


class Deadline:
    __slots__ = ("budget_seconds", "_started")

    def __init__(self, budget_seconds: float) -> None:
        if budget_seconds <= 0:
            raise ValueError("a deadline needs a positive budget")
        self.budget_seconds = budget_seconds
        self._started = time.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    def raise_if_expired(self, tool: str) -> None:
        if self.elapsed_seconds > self.budget_seconds:
            raise ToolTimeout(tool, self.elapsed_seconds, self.budget_seconds)


@dataclass
class ToolContext:
    """Who the call runs as. Constructed from a token, never from an identifier.

    This is the authorization boundary. There is no constructor that takes a user id or a
    profile id, because the only way to offer one is to accept one -- and the caller on the
    other side of this registry will eventually be a language model reading a student's own
    words back to the server.
    """

    user: User
    db: Session
    profile: Optional[StudentProfile] = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)

    @classmethod
    def authenticate(cls, token: str, db: Session, *, cancellation: Optional[CancellationToken] = None) -> "ToolContext":
        """Resolve a bearer token to a context, or raise `ToolUnauthenticated`.

        Delegates to `app.api.deps.authenticate_access_token` -- the same function the HTTP
        routes and the VA-2 WebSocket gateway use. A third implementation here would be a
        third set of rules to keep in step, and the one that drifted would be an
        authentication bypass no test of the other two could see.
        """
        from fastapi import HTTPException

        from app.api.deps import authenticate_access_token

        if not token or not token.strip():
            raise ToolUnauthenticated("no credentials were supplied")
        try:
            user = authenticate_access_token(token, db)
        except HTTPException as exc:
            # One opaque message for every cause, matching the HTTP path: distinguishing
            # "expired" from "revoked" from "no such user" turns this into an oracle.
            raise ToolUnauthenticated("invalid or expired credentials") from exc
        return cls(user=user, db=db, cancellation=cancellation or CancellationToken())

    def require_profile(self) -> StudentProfile:
        """The caller's profile, looked up read-only.

        Cached on the context so one invocation does not query twice, and **never** created --
        see `ProfileNotReady`.
        """
        if self.profile is None:
            self.profile = (
                self.db.query(StudentProfile).filter(StudentProfile.user_id == self.user.id).first()
            )
        if self.profile is None:
            raise ProfileNotReady()
        return self.profile

    def is_premium(self) -> bool:
        """Reuses the existing subscription check rather than re-deriving entitlement."""
        from app.services.subscription_service import is_premium

        return bool(is_premium(self.db, self.require_profile().id))


# --------------------------------------------------------------------------- schemas


class ToolInput(BaseModel):
    """Base for every tool's arguments.

    `extra="forbid"` is the point of it. A model that invents `profile_id`, `user_id` or
    `limit_override` gets a typed rejection instead of having the field quietly ignored --
    "ignored" being indistinguishable from "honoured" from the caller's side, and the thing a
    prompt-injection attempt would try first.
    """

    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    """Base for every tool's result.

    Outputs are purpose-built here rather than reusing the API response schemas. Those carry
    fields a spoken assistant has no business with -- an email address, a mobile number, a file
    URL -- and reusing them would mean every future field added for the UI is also exposed to a
    model by default. Opting in field by field is the only version of data minimisation that
    survives the next feature.
    """

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ActionPreview:
    """What an action would do, resolved against live state, before anything is confirmed.

    Produced by the tool's own `preview` callable and used for two different jobs, which is why
    it is one object rather than two. It is what the student is shown, and it is what the
    confirmation binds to -- so the sentence on the screen and the sentence the server will act
    on cannot diverge.

    `resource_key` is the stable identity of the thing being changed ("job_listing:41"). It is
    part of the confirmation binding, so a confirmation minted for one job cannot execute
    against another even if the argument shape is identical.
    """

    description: str
    resource_key: str
    resource_label: str
    target_state: str
    #: True when live state already satisfies the target. Not an error and not a reason to skip
    #: confirmation: the student still asked, and telling them it is already done is a better
    #: answer than silently doing nothing. The executor uses it to avoid a pointless write.
    already_satisfied: bool = False

    def __post_init__(self) -> None:
        if not self.description.strip() or not self.resource_key.strip():
            raise ValueError("an action preview needs a description and a resource key")


@dataclass(frozen=True)
class ActionOutcome:
    """The result of an execution attempt, as a state rather than a boolean.

    `verified` is only ever True alongside `SUCCEEDED`, and `SUCCEEDED` is only ever reached
    through an authoritative read -- see `ToolRegistry.invoke_action`. A payload that says
    succeeded without saying verified is not reachable, which is the point: "the request
    returned 200" and "the job is saved" are different facts and the client must not be able to
    confuse them.
    """

    capability: str
    state: ActionState
    verified: bool
    description: str
    resource_label: str
    #: Safe, redacted. Never an exception's text, never a database message.
    detail: Optional[str] = None
    #: True when the student's intent was already satisfied and nothing was written.
    no_change_needed: bool = False
    #: True when a duplicate submission was recognised and the first one's effect stands.
    replayed: bool = False

    def __post_init__(self) -> None:
        if self.verified and self.state is not ActionState.SUCCEEDED:
            raise ValueError("only a succeeded outcome may be reported as verified")
        if self.state is ActionState.SUCCEEDED and not self.verified:
            raise ValueError("a succeeded outcome must be verified against authoritative state")

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "state": self.state.value,
            "verified": self.verified,
            "description": self.description,
            "resource": self.resource_label,
            "detail": self.detail,
            "no_change_needed": self.no_change_needed,
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class ToolSpec:
    """One registered capability.

    Every field here is something a caller or a reviewer would otherwise have to infer from
    the handler's body.
    """

    name: str
    kind: ToolKind
    summary: str
    input_model: type[ToolInput]
    output_model: type[ToolOutput]
    handler: Callable[[ToolContext, Any], ToolOutput]
    #: Server-side entitlement, enforced in `ToolRegistry.invoke`. Mirrors what the HTTP
    #: routes already gate; a registry that ignored it would be a paywall bypass.
    requires_premium: bool = False
    #: Human-readable side effects on domain or user state. A READ tool must declare none,
    #: and that is enforced.
    side_effects: tuple[str, ...] = ()
    #: Writes a READ tool makes to *infrastructure metering* state, named by ORM class.
    #:
    #: Added in VA-5 for a real case rather than for symmetry. A world-intelligence tool spends
    #: an outbound provider call, and `app/services/job_hunt/provider_budget.py` records that
    #: against a shared per-day counter -- a `ProviderCallBudget` row. That is a genuine write,
    #: so declaring `side_effects=()` and nothing else would have been false.
    #:
    #: It is kept distinct from `side_effects` because the two differ in what they can harm.
    #: A domain side effect changes something a student owns and would have to be confirmed. A
    #: metering write changes only the deployment's own view of how much quota it has used; it
    #: carries no user data, is invisible to the student, and skipping it is what would do the
    #: harm -- an unmetered call overspends a shared API key. So READ permits these, and the
    #: read-only test asserts that a READ tool's writes are confined to exactly what it
    #: declared here. "No writes at all" would have been a tidier claim and a false one.
    metering_effects: tuple[str, ...] = ()
    idempotency: Idempotency = Idempotency.NATURAL
    verification: Verification = Verification.NONE
    #: VA-7. Required for every non-READ tool; meaningless for a READ one.
    risk_class: Optional[RiskClass] = None
    #: VA-7. Resolves the action against live state and returns what it would do. Run twice --
    #: once to mint the confirmation and once again at execution time -- so an authorization or
    #: business rule that changed in between is caught by the second run rather than trusted
    #: from the first.
    preview: Optional[Callable[[ToolContext, Any], "ActionPreview"]] = None
    #: VA-7. "Is the intended state now true?", read from an authoritative source. Lives on the
    #: spec rather than inside the handler so the registry can refuse to report success without
    #: it -- a handler that returned a success-shaped output would otherwise be believed.
    verify: Optional[Callable[[ToolContext, Any], bool]] = None
    #: Hard ceiling on list length in the output. Truncation is reported, never silent.
    max_items: int = 20
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.name or " " in self.name:
            raise ValueError(f"tool name {self.name!r} must be a stable, space-free identifier")
        if self.timeout_seconds <= 0 or self.max_items <= 0:
            raise ValueError(f"{self.name}: timeout and max_items must be positive")
        if self.kind is ToolKind.READ:
            # The guarantee the whole read-only phase rests on. If a READ tool could declare a
            # side effect, "read-only" would be a naming convention rather than a property,
            # and the registry would have no basis for running it without confirmation.
            if self.side_effects:
                raise ValueError(f"{self.name} is READ but declares side effects: {self.side_effects}")
            if self.idempotency is not Idempotency.NATURAL:
                raise ValueError(f"{self.name} is READ, so repeating it cannot change anything")
            if self.verification is not Verification.NONE:
                raise ValueError(f"{self.name} is READ and cannot require confirmation")
            # A preview and a verifier only mean anything for something that changes state.
            # Allowing them on a READ tool would make the classification ambiguous at exactly
            # the point where other code branches on it.
            if self.preview is not None or self.verify is not None:
                raise ValueError(f"{self.name} is READ and must not declare a preview or a verifier")
            if self.risk_class is not None:
                raise ValueError(f"{self.name} is READ, so it has no action risk class")
        else:
            if self.verification is Verification.NONE:
                raise ValueError(f"{self.name} is {self.kind.value} and must declare a verification requirement")
            if self.idempotency is Idempotency.NATURAL:
                raise ValueError(f"{self.name} is {self.kind.value}; declare its real idempotency policy")
            if self.risk_class is None:
                raise ValueError(f"{self.name} is {self.kind.value} and must declare a risk class")
            # Both are structural, not optional extras. Without a preview there is nothing for a
            # confirmation to bind to; without a verifier the only evidence of success would be
            # the handler's own say-so.
            if self.preview is None:
                raise ValueError(f"{self.name} is {self.kind.value} and must declare a preview")
            if self.verify is None:
                raise ValueError(f"{self.name} is {self.kind.value} and must declare an authoritative verifier")

    def describe(self) -> dict[str, Any]:
        """The orchestrator-facing description.

        Provider-neutral by construction: a plain dict of primitives plus a JSON Schema. The
        translation into any one vendor's function-calling format is the orchestrator's job,
        and doing it here would bind the registry to whichever vendor was chosen first.
        """
        return {
            "name": self.name,
            "kind": self.kind.value,
            "summary": self.summary,
            "input_schema": self.input_model.model_json_schema(),
            "requires_premium": self.requires_premium,
            "side_effects": list(self.side_effects),
            "metering_effects": list(self.metering_effects),
            "idempotency": self.idempotency.value,
            "verification": self.verification.value,
            "risk_class": self.risk_class.value if self.risk_class else None,
            "max_items": self.max_items,
        }


@dataclass(frozen=True)
class ToolResult:
    """A successful invocation."""

    name: str
    kind: ToolKind
    output: ToolOutput
    duration_seconds: float
    #: True when a list in the output hit `max_items`. Surfaced so a caller can say "and more"
    #: rather than present a truncated list as complete.
    truncated: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability": self.name,
            "kind": self.kind.value,
            "truncated": self.truncated,
            "result": self.output.model_dump(mode="json"),
        }
