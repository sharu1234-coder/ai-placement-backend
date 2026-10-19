"""The registry itself: register, enumerate, invoke.

One class, and the whole security argument lives in `invoke`. The order of its steps is the
design: every refusal that can be made without touching the database is made before the
database is touched, and every refusal that can be made without running the handler is made
before the handler runs.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Mapping, Optional

from pydantic import ValidationError

from app.tools.contracts import (
    ActionOutcome,
    ActionPreview,
    ActionState,
    Deadline,
    ToolConfirmationInvalid,
    ToolConfirmationRequired,
    ToolContext,
    ToolError,
    ToolExecutionFailed,
    ToolIdempotencyKeyRequired,
    ToolInput,
    ToolInputInvalid,
    ToolKind,
    ToolNotFound,
    ToolPermissionDenied,
    ToolResult,
    ToolSpec,
    ToolTimeout,
    ToolUnauthenticated,
)

logger = logging.getLogger(__name__)

#: Ceiling on one serialised result, in characters. A bounded output schema bounds the shape;
#: this bounds the size, because a schema with a list of strings is still unbounded in bytes.
#: Anything over it is a bug in a tool's own limits rather than something to trim silently.
MAX_RESULT_CHARACTERS = 20_000


class ToolRegistry:
    """A catalogue of capabilities. Not a singleton: tests build their own."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ registration

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            # A duplicate name would make which implementation runs depend on import order.
            raise ValueError(f"a capability named '{spec.name}' is already registered")
        self._tools[spec.name] = spec
        return spec

    def register_all(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFound(name) from None

    # ------------------------------------------------------------------ enumeration

    def describe_for(self, context: ToolContext) -> list[dict[str, Any]]:
        """The capabilities this caller may actually invoke.

        Requires a context, so there is no anonymous enumeration: the catalogue is a map of
        what the product can do with a student's data, and handing it out unauthenticated
        would be a free design document for anyone probing the API.

        A premium-only tool is **omitted** for a free account rather than listed as locked.
        Listing it would put a capability in a model's tool list that the model would then
        try, fail, and have to explain -- and the paywall is the app's job to present, in its
        own UI, not a voice agent's.
        """
        # Checked explicitly rather than left to whichever tool happens to need the context
        # first. Without this the guarantee is accidental: a catalogue with nothing gated
        # would enumerate happily for a caller that has no identity at all, and the day a
        # gated tool is added is the day that silently changes.
        if context is None or getattr(context, "user", None) is None:
            raise ToolUnauthenticated("the capability catalogue requires an authenticated caller")

        premium: Optional[bool] = None
        visible = []
        for spec in sorted(self._tools.values(), key=lambda s: s.name):
            if spec.requires_premium:
                if premium is None:
                    premium = context.is_premium()
                if not premium:
                    continue
            visible.append(spec.describe())
        return visible

    # ------------------------------------------------------------------ invocation

    def invoke(
        self,
        name: str,
        arguments: Optional[Mapping[str, Any]],
        context: ToolContext,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> ToolResult:
        """Run one capability.

        Steps, in this order and for these reasons:

        1. **Resolve the name.** An unregistered capability never reaches anything else, and
           the refusal does not reveal what *is* registered.
        2. **Check entitlement.** Before the arguments are parsed, so a premium tool cannot be
           probed for its schema by a free account reading the validation errors.
        3. **Validate arguments** against the tool's own model, with unknown fields rejected.
        4. **Check cancellation**, then start the clock.
        5. **Run the handler**, with every non-`ToolError` exception wrapped and redacted.
        6. **Validate and bound the output**, so a handler cannot return a shape -- or a size
           -- the contract did not promise.

        VA-7 narrowed this to READ. A mutation reached through here would have skipped
        confirmation, idempotency and verification, and "the caller remembered to use the other
        method" is not a boundary. `invoke_action` is the write path and it is the only caller
        of the shared dispatch below that may run a non-READ tool.
        """
        spec = self.get(name)
        if spec.kind is not ToolKind.READ:
            raise ToolPermissionDenied(
                spec.name, f"it is classified {spec.kind.value} and must run through the confirmed action path"
            )
        return self._dispatch(spec, arguments, context, timeout_seconds=timeout_seconds)

    def _dispatch(
        self,
        spec: ToolSpec,
        arguments: Optional[Mapping[str, Any]],
        context: ToolContext,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> ToolResult:
        """The shared execution body. Private, because its only safety property is the order of
        its steps -- it makes no judgement about whether this tool was allowed to run at all."""
        budget = timeout_seconds or spec.timeout_seconds

        if spec.requires_premium and not context.is_premium():
            raise ToolPermissionDenied(spec.name, "it requires an active Premium subscription")

        try:
            parsed = spec.input_model.model_validate(dict(arguments or {}))
        except ValidationError as exc:
            raise ToolInputInvalid(spec.name, exc) from exc

        context.cancellation.raise_if_cancelled(spec.name, "starting")
        deadline = Deadline(budget)

        # Identity, byte counts and outcomes only -- never arguments and never results. A tool
        # call's arguments and output are derived from a student's own data, and this line runs
        # on every call.
        logger.info("tool %s invoked by user=%s", spec.name, context.user.id)

        started = time.monotonic()
        try:
            output = spec.handler(context, parsed)
        except ToolError:
            # Already the right error, already redacted. Re-wrapping a permission denial as an
            # execution failure would lose the distinction a caller needs.
            raise
        except Exception as exc:
            logger.warning("tool %s failed for user=%s: %s", spec.name, context.user.id, type(exc).__name__)
            raise ToolExecutionFailed(spec.name, exc) from exc
        duration = time.monotonic() - started

        # Reported after the fact rather than enforced mid-query: a synchronous database call
        # cannot be interrupted from here, and claiming otherwise would be the dishonest part.
        deadline.raise_if_expired(spec.name)

        if not isinstance(output, spec.output_model):
            raise ToolExecutionFailed(
                spec.name, TypeError(f"handler returned {type(output).__name__}, not {spec.output_model.__name__}")
            )

        truncated = bool(getattr(output, "truncated", False))
        payload = output.model_dump_json()
        if len(payload) > MAX_RESULT_CHARACTERS:
            raise ToolExecutionFailed(
                spec.name,
                ValueError(f"result of {len(payload)} characters exceeds the {MAX_RESULT_CHARACTERS} ceiling"),
            )

        return ToolResult(
            name=spec.name, kind=spec.kind, output=output, duration_seconds=duration, truncated=truncated
        )

    def invoke_read(self, name: str, arguments: Optional[Mapping[str, Any]], context: ToolContext) -> ToolResult:
        """Invoke, refusing anything that is not classified READ.

        The entry point for VA-4 and for any caller that must not be able to cause a change --
        a voice orchestrator before VA-7 exists, most of all. It is a separate method rather
        than a flag because a flag defaults to something, and the safe default here is the one
        a caller cannot forget to pass.
        """
        spec = self.get(name)
        if spec.kind is not ToolKind.READ:
            raise ToolPermissionDenied(spec.name, f"it is classified {spec.kind.value} and write tools are not enabled")
        return self.invoke(name, arguments, context)

    # ------------------------------------------------------------------ actions (VA-7)

    def _prepare_action(
        self, name: str, arguments: Optional[Mapping[str, Any]], context: ToolContext
    ) -> tuple[ToolSpec, ToolInput, str]:
        """The checks both action steps share, in the same order, with the same refusals.

        Shared rather than written twice on purpose: a preview that applied a weaker rule than
        the execute would be the gap worth attacking, and two copies of five checks is how that
        gap appears. Returns the spec, the validated arguments and their canonical fingerprint.
        """
        spec = self.get(name)
        if spec.kind is ToolKind.READ:
            raise ToolPermissionDenied(spec.name, "it is a read tool; use the read entry point")

        if spec.requires_premium and not context.is_premium():
            raise ToolPermissionDenied(spec.name, "it requires an active Premium subscription")

        try:
            parsed = spec.input_model.model_validate(dict(arguments or {}))
        except ValidationError as exc:
            raise ToolInputInvalid(spec.name, exc) from exc

        # Before anything is written, and before a confirmation is minted or burned: a
        # cancelled request must not mutate, and that is only true if the check happens here
        # rather than after the handler has been entered.
        context.cancellation.raise_if_cancelled(spec.name, "preparing")

        from app.services.idempotency import fingerprint

        # The same canonicaliser the HTTP idempotency guard uses, so "the arguments changed"
        # means one thing across both mechanisms. Dumped in JSON mode so a value's type is part
        # of its identity -- 5 and "5" are different requests.
        canonical = fingerprint(spec.name, parsed.model_dump(mode="json"))
        return spec, parsed, canonical

    def preview_action(
        self,
        name: str,
        arguments: Optional[Mapping[str, Any]],
        context: ToolContext,
        *,
        confirmations: Optional[Any] = None,
    ) -> tuple[str, Any, ActionPreview]:
        """Resolve what the action would do and mint a single-use confirmation for exactly that.

        Returns `(token, grant, preview)`. The token is returned once, here, and the caller is
        responsible for handing it to the student's own client and nowhere else.

        Nothing is written. The preview runs the tool's own resolution, which includes its
        ownership check, so a job that is not this student's is refused at this step with the
        same undisclosing error the execute step would give.
        """
        spec, parsed, canonical = self._prepare_action(name, arguments, context)
        preview = spec.preview(context, parsed)

        from app.tools.confirmation import store as default_store

        # `is not None`, not `or`: a ConfirmationStore defines __len__, so an empty one is
        # falsy and `or` silently substituted the process-wide store for the caller's. Found by
        # a test that minted a grant in one store and could not find it there.
        chosen = confirmations if confirmations is not None else default_store
        token, grant = chosen.issue(
            user_id=context.user.id,
            capability=spec.name,
            argument_fingerprint=canonical,
            resource_key=preview.resource_key,
            resource_label=preview.resource_label,
            description=preview.description,
            risk_class=spec.risk_class,
        )
        logger.info("action %s previewed by user=%s", spec.name, context.user.id)
        return token, grant, preview

    def invoke_action(
        self,
        name: str,
        arguments: Optional[Mapping[str, Any]],
        context: ToolContext,
        *,
        confirmation_token: Optional[str],
        idempotency_key: Optional[str] = None,
        confirmations: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
    ) -> ActionOutcome:
        """Run one confirmed mutation, or explain in a typed state why it did not run.

        The order, and why each step is where it is:

        1. **Re-run every preparation check.** Classification, entitlement, argument schema and
           cancellation, all again. The preview's verdict is a fact about a moment that has
           passed.
        2. **Consume the confirmation.** Single-use, and burned before the handler is entered,
           so a request that races this one finds nothing to spend.
        3. **Re-run the preview.** This is the execution-time revalidation: ownership and the
           tool's own business rules are evaluated against live state, not inherited from the
           grant. The resource identity is compared with what was approved, so a grant for one
           job cannot be spent on another.
        4. **Skip the write when the target state already holds.** Verified rather than assumed,
           and reported as `no_change_needed`.
        5. **Execute inside the existing operation-claim guard**, so a duplicate submission
           collapses to one mutation across workers.
        6. **Verify from an authoritative read.** Success is unreachable without it.
        """
        spec, parsed, canonical = self._prepare_action(name, arguments, context)

        from app.tools.contracts import Idempotency

        # Checked before the confirmation is consumed, so a client bug cannot burn a grant the
        # student would then have to re-approve.
        if spec.idempotency is Idempotency.KEY_REQUIRED and not (idempotency_key or "").strip():
            raise ToolIdempotencyKeyRequired(spec.name)

        if not confirmation_token or not confirmation_token.strip():
            raise ToolConfirmationRequired(spec.name)

        # Ahead of consumption, for the same reason the two checks above are: a cancelled call
        # that burned the grant first would leave the student re-approving an action they had
        # already approved, and whoever can trigger a cancellation could deny them it. This is
        # also the last honest place to check. Past this line the approval is spent and the
        # claim is being taken, so `CancellationToken`'s documented guarantee -- that a
        # cancelled call does not *start* -- is exactly what is enforced here and no more.
        context.cancellation.raise_if_cancelled(spec.name, "about to execute")

        from app.tools.confirmation import store as default_store

        chosen = confirmations if confirmations is not None else default_store
        grant = chosen.consume(
            confirmation_token,
            user_id=context.user.id,
            capability=spec.name,
            argument_fingerprint=canonical,
        )

        preview = spec.preview(context, parsed)
        if preview.resource_key != grant.resource_key:
            # The arguments hash the same but resolve to something else, which means the world
            # moved under the approval. Refusing is the only safe reading: the student approved
            # a sentence about a specific thing.
            raise ToolConfirmationInvalid(spec.name)

        if preview.already_satisfied:
            # Nothing to do, and saying so beats writing to prove it. Still verified, because
            # "already true" is a claim about live state like any other.
            return self._verified_outcome(
                spec, parsed, context, preview, no_change_needed=True, replayed=False
            )

        return self._execute_action(spec, parsed, arguments, context, preview, idempotency_key, timeout_seconds)

    def _execute_action(
        self,
        spec: ToolSpec,
        parsed: ToolInput,
        arguments: Optional[Mapping[str, Any]],
        context: ToolContext,
        preview: ActionPreview,
        idempotency_key: Optional[str],
        timeout_seconds: Optional[float],
    ) -> ActionOutcome:
        from app.services import idempotency as idem

        profile = context.require_profile()
        params = parsed.model_dump(mode="json")

        def perform() -> tuple[ToolResult, Optional[int]]:
            return self._dispatch(spec, arguments, context, timeout_seconds=timeout_seconds), None

        try:
            # `result_id=None` is deliberate. A replay is answered from the authoritative read
            # below rather than from a recorded row id, which is the only answer that stays
            # true for an action whose durable effect is a deletion -- there is no row left to
            # point at, and a mechanism that needed one would have to pretend otherwise.
            produced = idem.run_once(
                context.db,
                profile.id,
                operation=f"tool.{spec.name}",
                idempotency_key=idempotency_key,
                params=params,
                perform=perform,
                read_back=lambda _result_id: None,
                result_kind="tool_action",
            )
        except idem.OperationInProgressError:
            # A duplicate arrived while the first one is still running. Starting a second
            # mutation is the one thing that must not happen, so this reports in-progress and
            # leaves the decision to look again with the caller.
            logger.info("duplicate submission of %s while in flight for user=%s", spec.name, context.user.id)
            return ActionOutcome(
                capability=spec.name,
                state=ActionState.IN_PROGRESS,
                verified=False,
                description=preview.description,
                resource_label=preview.resource_label,
                detail="The same request is already running. Check again in a moment rather than retrying.",
            )
        except idem.IdempotencyError:
            # A conflicting key, a malformed one, or a result that no longer exists. All of
            # these are refusals made *before* anything ran, so reconciling them as though a
            # mutation might have happened would invent an unknown outcome out of a clean
            # rejection. The route maps them to their existing status codes.
            raise
        except ToolExecutionFailed as exc:
            # The handler itself raised. Whether it committed first is not knowable from here,
            # so the authoritative read decides -- see `_reconcile`. This clause must precede
            # the ToolError one below, because ToolExecutionFailed is one.
            logger.warning("action %s raised for user=%s: %s", spec.name, context.user.id, type(exc.cause).__name__)
            return self._reconcile(spec, parsed, context, preview, cause=exc)
        except ToolTimeout as exc:
            # The deadline is checked *after* the handler returns, because a synchronous write
            # cannot be interrupted from here. So a timeout on an action means the mutation may
            # well have committed and simply taken too long -- raising it as a failure would
            # tell a student their change did not happen when the row is already there.
            # Reconciled from the authoritative read for exactly that reason.
            logger.warning("action %s exceeded its budget for user=%s", spec.name, context.user.id)
            return self._reconcile(spec, parsed, context, preview, cause=exc)
        except ToolError:
            # Typed, redacted, and raised before the mutation: a permission denial, a missing
            # resource, invalid arguments. The claim was released by `run_once`, so an explicit
            # retry is permitted.
            raise
        except Exception as exc:
            # Something failed around the mutation and whether it committed is not knowable
            # from here. The authoritative read is what decides, and a blind retry never is.
            logger.warning("action %s failed for user=%s: %s", spec.name, context.user.id, type(exc).__name__)
            return self._reconcile(spec, parsed, context, preview, cause=exc)

        replayed = produced is None
        if replayed:
            logger.info("replayed submission of %s for user=%s", spec.name, context.user.id)
        return self._verified_outcome(
            spec, parsed, context, preview, no_change_needed=False, replayed=replayed
        )

    def _verified_outcome(
        self,
        spec: ToolSpec,
        parsed: ToolInput,
        context: ToolContext,
        preview: ActionPreview,
        *,
        no_change_needed: bool,
        replayed: bool,
    ) -> ActionOutcome:
        """Ask the authoritative source whether the intended state holds, and say only that.

        Nothing is raised from here. Once a mutation has been attempted, every answer is a
        state the caller has to render, and the three that matter -- it took effect, it did not,
        I could not tell -- are exactly what an exception would flatten into one.
        """
        try:
            reached = bool(spec.verify(context, parsed))
        except Exception as exc:
            logger.warning(
                "verification of %s failed to read for user=%s: %s", spec.name, context.user.id, type(exc).__name__
            )
            return ActionOutcome(
                capability=spec.name,
                state=ActionState.UNKNOWN,
                verified=False,
                description=preview.description,
                resource_label=preview.resource_label,
                detail=(
                    "This may or may not have been applied, and it could not be read back. "
                    "Check before trying again -- it will not be retried automatically."
                ),
                no_change_needed=no_change_needed,
                replayed=replayed,
            )

        if not reached:
            return ActionOutcome(
                capability=spec.name,
                state=ActionState.VERIFICATION_FAILED,
                verified=False,
                description=preview.description,
                resource_label=preview.resource_label,
                detail="The change could not be confirmed. Please check before trying again.",
                replayed=replayed,
            )
        return ActionOutcome(
            capability=spec.name,
            state=ActionState.SUCCEEDED,
            verified=True,
            description=preview.description,
            resource_label=preview.resource_label,
            detail="Already done; nothing needed changing." if no_change_needed else None,
            no_change_needed=no_change_needed,
            replayed=replayed,
        )

    def _reconcile(
        self,
        spec: ToolSpec,
        parsed: ToolInput,
        context: ToolContext,
        preview: ActionPreview,
        *,
        cause: BaseException,
    ) -> ActionOutcome:
        """Decide what actually happened after a failed attempt, from the authoritative read.

        Three outcomes and no fourth: the state was reached, so the mutation committed despite
        the error; it was not, so nothing durable happened and a retry is a genuine retry; or
        the read itself failed, which is `UNKNOWN` and must not be retried automatically.
        """
        try:
            reached = bool(spec.verify(context, parsed))
        except Exception:
            logger.warning(
                "could not reconcile %s for user=%s after %s", spec.name, context.user.id, type(cause).__name__
            )
            return ActionOutcome(
                capability=spec.name,
                state=ActionState.UNKNOWN,
                verified=False,
                description=preview.description,
                resource_label=preview.resource_label,
                detail=(
                    "This may or may not have been applied, and it could not be read back. "
                    "Check before trying again -- it will not be retried automatically."
                ),
            )
        if reached:
            return ActionOutcome(
                capability=spec.name,
                state=ActionState.SUCCEEDED,
                verified=True,
                description=preview.description,
                resource_label=preview.resource_label,
                detail="Reported an error while running, but the change is confirmed to have taken effect.",
            )
        logger.info("reconciled %s to failed for user=%s after %s", spec.name, context.user.id, type(cause).__name__)
        return ActionOutcome(
            capability=spec.name,
            state=ActionState.FAILED,
            verified=False,
            description=preview.description,
            resource_label=preview.resource_label,
            # The cause is logged, never rendered: see ToolExecutionFailed.
            detail=f"{spec.name} could not be completed and nothing was changed.",
        )


def build_default_registry(*, include_world: bool = True, include_actions: bool = False) -> ToolRegistry:
    """The catalogue: VA-4's personal read tools plus VA-5's world read tools.

    `include_world=False` builds the personal-only catalogue. Useful for a caller that must not
    make outbound calls at all, and for testing the two groups independently -- the world tools
    reach an external provider and so can fail in ways a database query cannot.

    `include_actions` defaults to **False**, so VA-7's write tools are absent unless a caller
    asks for them. Deliberate rather than cautious-by-habit: the VA-6 orchestrator builds this
    catalogue, and a write tool appearing in it by default would put a mutation in a model's
    tool list as a side effect of a registry change nobody reviewed. The action route asks for
    them explicitly, which is one grep away from being auditable.
    """
    from app.tools.read_tools import read_only_specs

    registry = ToolRegistry()
    registry.register_all(read_only_specs())
    if include_world:
        from app.tools.world_tools import world_specs

        registry.register_all(world_specs())
    if include_actions:
        from app.tools.action_tools import action_specs

        registry.register_all(action_specs())
    return registry
