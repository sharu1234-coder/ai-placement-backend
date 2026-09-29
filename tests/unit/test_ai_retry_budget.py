"""F12-06: one logical AI operation has one bounded retry budget.

These tests sit at the real abstraction seam. The counter is always on the wire -- `httpx.post`
for Ollama, an `httpx.MockTransport` underneath a real `anthropic.Anthropic` client for Anthropic
-- because "how many provider calls did one logical operation make" is a question only the wire
can answer. Asserting on a mock of the provider would prove nothing about the layers underneath
it, which is exactly where the multiplication lived.

What was measured on that wire before this phase, and what these tests now pin down:

    Ollama    : 6 provider calls, 6 x 240s = 1441s  ->  3 calls, <= 300s
    Anthropic : 16 provider calls, >= 1920s         ->  3 calls, <= 300s
    sustained 429 (Anthropic): 9 calls and 180.6s of sleeping -> 2 calls, one bounded wait

Wall clock is exercised through a virtual clock rather than by waiting. The clock is not there to
make the numbers look good -- it is the only way to play out an attempt that consumes its whole
240s timeout, which is the case that produced Phase 17's 617s explanation pass.
"""

import asyncio

import anthropic
import httpx
import pytest
from pydantic import BaseModel

import app.ai.ollama_provider as ollama_module
from app.ai import retry as retry_module
from app.ai.anthropic_provider import AnthropicProvider, _classify_anthropic_failure
from app.ai.ollama_provider import OllamaProvider, _classify_ollama_failure
from app.ai.provider import AIGenerationError
from app.ai.retry import (
    MAX_ATTEMPTS_PER_STAGE,
    MAX_PROVIDER_CALLS,
    NON_RETRYABLE_CATEGORIES,
    RETRY_BACKOFF_SECONDS,
    RETRYABLE_CATEGORIES,
    AIBudgetExhausted,
    AIOperationBudget,
    FailureCategory,
    FailureVerdict,
)
from app.core.config import settings


class Animal(BaseModel):
    name: str
    legs: int


GOOD_JSON = '{"name": "dog", "legs": 4}'
BAD_JSON = '{"name": "dog"}'  # a 200 with valid JSON of the wrong shape


# --------------------------------------------------------------------------------------
# the virtual clock
# --------------------------------------------------------------------------------------


class Clock:
    """Drives app.ai.retry's own notion of time. `advance` also stands in for time.sleep, so a
    backoff is recorded as elapsed budget instead of stalling the suite."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.advance(seconds)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(retry_module.time, "monotonic", lambda: c.now)
    monkeypatch.setattr(retry_module.time, "sleep", c.sleep)
    return c


def always(verdict: FailureVerdict):
    return lambda _exc: verdict


TRANSIENT = always(FailureVerdict(FailureCategory.TRANSIENT_NETWORK))
PERMANENT = always(FailureVerdict(FailureCategory.CLIENT_ERROR))


def budget(total_seconds=300.0, per_attempt_seconds=240.0, operation="TestOperation"):
    return AIOperationBudget(
        operation=operation, provider="test",
        per_attempt_seconds=per_attempt_seconds, total_seconds=total_seconds,
    )


# --------------------------------------------------------------------------------------
# the budget itself
# --------------------------------------------------------------------------------------


def test_a_successful_operation_makes_exactly_one_provider_call(clock):
    calls = []
    with budget() as b:
        assert b.call(lambda t: calls.append(t) or "done", classify=TRANSIENT, stage="initial") == "done"
    assert len(calls) == 1
    assert b.calls == 1


def test_a_transient_failure_is_retried_exactly_once_and_no_more(clock):
    calls = []

    def flaky(_timeout):
        calls.append(1)
        raise RuntimeError("blip")

    with pytest.raises(RuntimeError), budget() as b:
        b.call(flaky, classify=TRANSIENT, stage="initial")
    # 1 initial attempt + exactly 1 retry. The old policy made 3 here, and the SDK could turn
    # each of those into 3 more.
    assert len(calls) == MAX_ATTEMPTS_PER_STAGE == 2


def test_a_transient_failure_that_clears_succeeds_on_the_retry(clock):
    calls = []

    def flaky(_timeout):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("blip")
        return "recovered"

    with budget() as b:
        assert b.call(flaky, classify=TRANSIENT, stage="initial") == "recovered"
    assert len(calls) == 2


def test_a_non_retryable_failure_is_never_retried(clock):
    calls = []

    def permanent(_timeout):
        calls.append(1)
        raise RuntimeError("bad request")

    with pytest.raises(RuntimeError), budget() as b:
        b.call(permanent, classify=PERMANENT, stage="initial")
    assert len(calls) == 1


@pytest.mark.parametrize("category", sorted(RETRYABLE_CATEGORIES, key=lambda c: c.value))
def test_every_retryable_category_gets_exactly_one_retry(clock, category):
    calls = []

    def failing(_timeout):
        calls.append(1)
        raise RuntimeError(category.value)

    with pytest.raises(RuntimeError), budget() as b:
        b.call(failing, classify=always(FailureVerdict(category)), stage="initial")
    assert len(calls) == 2, f"{category.value} should be retried exactly once"


@pytest.mark.parametrize("category", sorted(NON_RETRYABLE_CATEGORIES, key=lambda c: c.value))
def test_every_non_retryable_category_is_refused_a_retry(clock, category):
    calls = []

    def failing(_timeout):
        calls.append(1)
        raise RuntimeError(category.value)

    with pytest.raises(RuntimeError), budget() as b:
        b.call(failing, classify=always(FailureVerdict(category)), stage="initial")
    assert len(calls) == 1, f"{category.value} must not be retried"


def test_the_two_category_sets_partition_every_failure_category():
    """No category may be in both sets or in neither: a category that fell out of both would
    silently take whichever branch the code happens to reach."""
    assert RETRYABLE_CATEGORIES | NON_RETRYABLE_CATEGORIES == set(FailureCategory)
    assert RETRYABLE_CATEGORIES & NON_RETRYABLE_CATEGORIES == set()
    # The specific classification the audit settled on, spelled out so a change to it is a
    # deliberate edit to this list rather than a quiet drift.
    assert {c.value for c in RETRYABLE_CATEGORIES} == {
        "transient_network", "timeout", "rate_limit", "server_error"
    }
    assert {c.value for c in NON_RETRYABLE_CATEGORIES} == {
        "auth", "client_error", "schema", "cancelled", "unknown"
    }


def test_the_backoff_is_a_single_fixed_wait(clock):
    """Bounded and deterministic, so the total wall clock of a retrying operation is a number a
    test can assert rather than a growth curve to reproduce."""
    def failing(_timeout):
        raise RuntimeError("blip")

    with pytest.raises(RuntimeError), budget() as b:
        b.call(failing, classify=TRANSIENT, stage="initial")
    assert clock.slept == pytest.approx(RETRY_BACKOFF_SECONDS)
    assert RETRY_BACKOFF_SECONDS <= 1.0


def test_a_servers_retry_after_replaces_the_fixed_backoff(clock):
    """The anthropic SDK's retry loop is disabled, and it was the only layer honouring this
    header, so this layer has to."""
    def rate_limited(_timeout):
        raise RuntimeError("429")

    with pytest.raises(RuntimeError), budget() as b:
        b.call(rate_limited,
               classify=always(FailureVerdict(FailureCategory.RATE_LIMIT, 30.0)),
               stage="initial")
    assert clock.slept == pytest.approx(30.0)
    assert b.calls == 2  # waited, retried once, and stopped -- not a storm


def test_a_retry_after_that_does_not_fit_the_budget_is_not_retried(clock):
    """A 900s Retry-After inside a 300s budget: waiting it out would blow the deadline, so the
    operation gives up immediately instead of sleeping through it."""
    calls = []

    def rate_limited(_timeout):
        calls.append(1)
        raise RuntimeError("429")

    with pytest.raises(RuntimeError), budget() as b:
        b.call(rate_limited,
               classify=always(FailureVerdict(FailureCategory.RATE_LIMIT, 900.0)),
               stage="initial")
    assert len(calls) == 1
    assert clock.slept == 0.0


def test_each_attempts_timeout_is_clamped_to_the_remaining_budget(clock):
    """Half of how the deadline is enforced: no single attempt may be allowed to run past it."""
    seen = []

    def slow(timeout):
        seen.append(timeout)
        clock.advance(200.0)
        raise RuntimeError("blip")

    with pytest.raises(RuntimeError), budget(total_seconds=300.0, per_attempt_seconds=240.0) as b:
        b.call(slow, classify=TRANSIENT, stage="initial")
    # The first attempt gets the full per-attempt timeout; there is no second, because 240s no
    # longer fits in the 99.5s left (see the next test).
    assert seen == [240.0]


def test_a_retry_is_refused_when_its_full_timeout_no_longer_fits(clock):
    """The other half, and the actual fix for Phase 17's 617s explanation pass: 240000 + 200 +
    240000 + 400 + 136478 was three stacked attempts inside one logical operation. A read
    timeout that consumed its whole 240s leaves 60s, which cannot give a retry a fair chance,
    so no provider call is spent on one."""
    calls = []

    def times_out(timeout):
        calls.append(timeout)
        clock.advance(timeout)
        raise RuntimeError("read timed out")

    with pytest.raises(RuntimeError), budget(total_seconds=300.0, per_attempt_seconds=240.0) as b:
        b.call(times_out, classify=always(FailureVerdict(FailureCategory.TIMEOUT)), stage="initial")
    assert calls == [240.0]
    assert b.elapsed_seconds == pytest.approx(240.0)


def test_a_correction_after_a_slow_first_attempt_gets_only_what_is_left(clock):
    """The clamp doing its real job. A first attempt that took 250s of a 300s budget leaves the
    correction 50s, not another full 240s -- otherwise the operation would run to 490s and the
    deadline would be decorative."""
    seen = []

    def slow(timeout):
        seen.append(timeout)
        clock.advance(250.0)
        return "late"

    with budget(total_seconds=300.0, per_attempt_seconds=240.0) as b:
        b.call(slow, classify=TRANSIENT, stage="initial")
        b.call(slow, classify=TRANSIENT, stage="correction")
    assert seen == [240.0, 50.0]
    assert b.calls == 2


def test_a_cheap_transient_failure_still_gets_its_retry(clock):
    """The mirror of the test above, and the reason the rule needs no special-casing per failure
    kind: a connection refused at 0.4s leaves 299.6s, so 240s fits and the retry -- which is
    where retries genuinely pay -- still happens."""
    calls = []

    def refused(timeout):
        calls.append(timeout)
        clock.advance(0.4)
        raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError), budget(total_seconds=300.0, per_attempt_seconds=240.0) as b:
        b.call(refused, classify=TRANSIENT, stage="initial")
    assert calls == [240.0, 240.0]


def test_the_call_cap_bounds_the_whole_operation_across_every_stage(clock):
    """Two stages of two attempts each would be four calls; the cap is on the operation, not the
    stage, so the second stage gets whatever is left of the three and no more. This is what stops
    the schema correction from multiplying the budget."""
    calls = []

    def failing(_timeout):
        calls.append(1)
        raise RuntimeError("blip")

    with budget() as b:
        with pytest.raises(RuntimeError):
            b.call(failing, classify=TRANSIENT, stage="initial")
        assert len(calls) == 2, "the first stage takes its 1 attempt + 1 retry"

        # The third call is allowed -- it is the correction's single attempt -- but its retry is
        # refused, because the operation has now spent the whole cap.
        with pytest.raises(RuntimeError):
            b.call(failing, classify=TRANSIENT, stage="correction")
        assert len(calls) == MAX_PROVIDER_CALLS == 3

        # Anything beyond the cap cannot even start.
        with pytest.raises(AIBudgetExhausted) as exhausted:
            b.call(failing, classify=TRANSIENT, stage="correction")
    assert len(calls) == 3, "no provider call may be made once the cap is spent"
    assert "call_cap" in str(exhausted.value)


def test_a_stage_cannot_start_once_the_deadline_has_passed(clock):
    def slow(timeout):
        clock.advance(timeout)
        return "late"

    with budget(total_seconds=300.0, per_attempt_seconds=300.0) as b:
        assert b.call(slow, classify=TRANSIENT, stage="initial") == "late"
        with pytest.raises(AIBudgetExhausted) as exhausted:
            b.call(slow, classify=TRANSIENT, stage="correction")
    assert "deadline" in str(exhausted.value)
    assert b.calls == 1


def test_budget_exhaustion_reaches_the_existing_no_ai_fallbacks():
    """AIBudgetExhausted is a subclass of AIGenerationError on purpose: all 19 AI call sites
    already fall back to a deterministic template on AIGenerationError, so a new failure mode
    needed no service-layer change anywhere and cannot escape as an unhandled 500."""
    assert issubclass(AIBudgetExhausted, AIGenerationError)


def test_cancellation_is_never_retried(clock):
    """A cancelled caller has gone away; another provider call would be work nobody is waiting
    for. CancelledError derives from BaseException so `except Exception` already misses it --
    this pins the invariant down so widening that handler cannot quietly undo it."""
    calls = []

    def cancelled(_timeout):
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError), budget() as b:
        # Classified retryable on purpose: cancellation must win over the classifier.
        b.call(cancelled, classify=TRANSIENT, stage="initial")
    assert len(calls) == 1


def test_the_budget_returns_the_providers_result_untouched(clock):
    """Nothing here may inspect, copy or rewrite a provider's output -- the output guards run on
    exactly the object the provider returned, so this layer must stay transparent to it."""
    sentinel = object()
    with budget() as b:
        assert b.call(lambda _t: sentinel, classify=TRANSIENT, stage="initial") is sentinel


def test_no_log_line_carries_the_prompt_or_the_response(clock, caplog):
    """Observability records the shape of the retry graph, never its payload."""
    secret_prompt = "CANDIDATE_RESUME_SECRET_abcdef123456"
    secret_response = "MODEL_OUTPUT_SECRET_zyxwvu654321"

    def failing(_timeout):
        raise RuntimeError(f"failed while sending {secret_prompt}")

    with caplog.at_level("DEBUG"):
        with pytest.raises(RuntimeError), budget(operation="ParsedResume") as b:
            b.call(failing, classify=TRANSIENT, stage="initial")
        with budget(operation="ParsedResume") as b2:
            b2.call(lambda _t: secret_response, classify=TRANSIENT, stage="initial")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_prompt not in logged
    assert secret_response not in logged
    # ...but the attempt shape is there, and each operation is distinguishable from the other.
    assert "ai_attempt" in logged and "ai_operation" in logged
    assert "op=ParsedResume" in logged
    assert f"id={b.id}" in logged and f"id={b2.id}" in logged
    assert b.id != b2.id


def test_the_default_wall_clock_budget_is_the_documented_one():
    """300s is not arbitrary: it is the Android client's own readTimeout(300, SECONDS). Above it
    the server holds a request its client has abandoned, which is how F12-06 stayed invisible."""
    assert settings.ai_total_budget_seconds == 300
    # It must leave room for a full attempt on either provider, or a normal slow success would
    # be cut short -- the measured successful range on this project's local model is 98.9-183.4s.
    assert settings.ai_total_budget_seconds >= settings.ollama_timeout_seconds
    assert settings.ai_total_budget_seconds >= settings.anthropic_timeout_seconds


# --------------------------------------------------------------------------------------
# no hidden retry layer underneath the budget
# --------------------------------------------------------------------------------------


def test_the_anthropic_sdk_retry_loop_is_disabled(monkeypatch):
    """The SDK's default is 2 retries, i.e. 3 wire calls for every one of the application's
    attempts. Left at its default it multiplied this app's own retry and its schema correction
    into 16 wire calls for one logical operation."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    provider = AnthropicProvider()
    assert provider._client.max_retries == 0
    assert anthropic._constants.DEFAULT_MAX_RETRIES == 2, (
        "the SDK default changed; the point of setting max_retries=0 explicitly is that this "
        "app's policy does not depend on it"
    )


def test_the_anthropic_sdk_really_makes_one_wire_call_per_attempt(monkeypatch, clock):
    """Not just that the setting is 0 -- that the wire agrees."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    wire = []

    def handler(_request):
        wire.append(1)
        return httpx.Response(503, json={"error": {"type": "x", "message": "y"}})

    provider = AnthropicProvider()
    provider._client = anthropic.Anthropic(
        api_key="test-key", timeout=5, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AIGenerationError):
        provider.generate_structured("p", Animal)
    # 2 application attempts, 1 wire call each. Before this phase: 2 x 3 = 6, and that was only
    # the first stage.
    assert len(wire) == MAX_ATTEMPTS_PER_STAGE == 2


def test_the_ollama_http_client_adds_no_retries_of_its_own():
    """httpx's transport-level retry defaults to 0 and this app never raises it, so there is no
    fifth layer hiding under the fourth. Asserted rather than assumed, because a future shared
    client with retries=N would be invisible at every call site."""
    import inspect

    from httpx._transports.default import HTTPTransport

    assert inspect.signature(HTTPTransport.__init__).parameters["retries"].default == 0
    assert httpx.HTTPTransport().__dict__.get("_pool").__dict__.get("_retries", 0) == 0


# --------------------------------------------------------------------------------------
# the same policy through both real providers
# --------------------------------------------------------------------------------------
#
# A "script" is provider-agnostic so one scenario can be played through both providers and their
# call counts compared directly. Item forms:
#     "ok"                     a 200 the schema accepts
#     "bad_schema"             a 200 the schema rejects
#     ("status", code, hdrs)   an HTTP error response
#     "network"                a connection-level failure
#     ("timeout", seconds)     an attempt that burns `seconds` and then times out


class Wire:
    def __init__(self):
        self.calls = 0
        self.timeouts = []


def _script_item(script, n):
    return script[min(n - 1, len(script) - 1)]


def drive_ollama(script, clock, total_seconds=300.0):
    wire = Wire()
    request = httpx.Request("POST", "http://127.0.0.1:0/api/generate")

    def post(_url, json=None, timeout=None):
        wire.calls += 1
        wire.timeouts.append(timeout)
        item = _script_item(script, wire.calls)
        if item == "ok" or item == "bad_schema":
            body = GOOD_JSON if item == "ok" else BAD_JSON
            return httpx.Response(200, json={"response": body}, request=request)
        if item == "network":
            raise httpx.ConnectError("connection refused")
        if isinstance(item, tuple) and item[0] == "timeout":
            clock.advance(item[1])
            raise httpx.ReadTimeout("read timed out")
        if isinstance(item, tuple) and item[0] == "status":
            response = httpx.Response(item[1], request=request, headers=item[2] if len(item) > 2 else {})
            raise httpx.HTTPStatusError(str(item[1]), request=request, response=response)
        raise AssertionError(f"bad script item {item!r}")

    original = ollama_module.httpx.post
    ollama_module.httpx.post = post
    try:
        provider = OllamaProvider(base_url="http://127.0.0.1:0", model="m")
        try:
            provider.generate_structured("p", Animal)
            wire.outcome = "ok"
        except AIGenerationError as exc:
            wire.outcome = type(exc).__name__
    finally:
        ollama_module.httpx.post = original
    return wire


def drive_anthropic(script, clock, total_seconds=300.0):
    wire = Wire()

    def message(text):
        return {"id": "m", "type": "message", "role": "assistant", "model": "m",
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}

    def handler(request):
        wire.calls += 1
        wire.timeouts.append(request.extensions.get("timeout", {}).get("read"))
        item = _script_item(script, wire.calls)
        if item == "ok":
            return httpx.Response(200, json=message(GOOD_JSON))
        if item == "bad_schema":
            return httpx.Response(200, json=message(BAD_JSON))
        if item == "network":
            raise httpx.ConnectError("connection refused")
        if isinstance(item, tuple) and item[0] == "timeout":
            clock.advance(item[1])
            raise httpx.ReadTimeout("read timed out")
        if isinstance(item, tuple) and item[0] == "status":
            return httpx.Response(item[1], headers=item[2] if len(item) > 2 else {},
                                  json={"error": {"type": "x", "message": "y"}})
        raise AssertionError(f"bad script item {item!r}")

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._client = anthropic.Anthropic(
        api_key="test-key", timeout=settings.anthropic_timeout_seconds, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.model = "m"
    try:
        provider.generate_structured("p", Animal)
        wire.outcome = "ok"
    except AIGenerationError as exc:
        wire.outcome = type(exc).__name__
    return wire


DRIVERS = {"ollama": drive_ollama, "anthropic": drive_anthropic}


@pytest.mark.parametrize("provider_name", sorted(DRIVERS))
@pytest.mark.parametrize(
    "label, script, expected_calls, expected_outcome",
    [
        ("success first time", ["ok"], 1, "ok"),
        ("network blip then success", ["network", "ok"], 2, "ok"),
        ("sustained network failure", ["network"], 2, "AIGenerationError"),
        ("sustained 503", [("status", 503)], 2, "AIGenerationError"),
        ("permanent 400", [("status", 400)], 1, "AIGenerationError"),
        ("auth failure 401", [("status", 401)], 1, "AIGenerationError"),
        ("auth failure 403", [("status", 403)], 1, "AIGenerationError"),
        ("schema failure twice", ["bad_schema"], 2, "AIGenerationError"),
        ("schema failure then a good correction", ["bad_schema", "ok"], 2, "ok"),
        ("schema failure, blip on the correction, then good",
         ["bad_schema", "network", "ok"], 3, "ok"),
        ("schema failure and a failing correction",
         ["bad_schema", "network"], 3, "AIGenerationError"),
        # The case that distinguishes one shared budget from a budget per stage: the initial
        # stage spends two calls, so the correction gets the one that is left and no retry. A
        # correction holding its own budget would make four calls here.
        ("blip, schema failure, then a failing correction",
         ["network", "bad_schema", "network"], 3, "AIGenerationError"),
    ],
)
def test_both_providers_apply_the_identical_policy(
    clock, provider_name, label, script, expected_calls, expected_outcome
):
    """One policy, not one per provider. The retry budget belongs to the shared layer, so a
    provider cannot opt out of it -- and none of these scenarios may exceed the call cap."""
    wire = DRIVERS[provider_name](script, clock)
    assert wire.calls == expected_calls, f"{provider_name}: {label}"
    assert wire.outcome == expected_outcome, f"{provider_name}: {label}"
    assert wire.calls <= MAX_PROVIDER_CALLS


@pytest.mark.parametrize("provider_name", sorted(DRIVERS))
def test_a_rate_limit_does_not_become_a_retry_storm(clock, provider_name):
    """429 with a Retry-After the budget can afford: wait once, retry once, stop. Before this
    phase the Anthropic path made 9 calls here and slept 180.6s, because the SDK honoured the
    header six times while the application layer retried on top of it."""
    wire = DRIVERS[provider_name]([("status", 429, {"Retry-After": "30"})], clock)
    assert wire.calls == 2
    assert clock.slept == pytest.approx(30.0)
    assert wire.outcome == "AIGenerationError"


@pytest.mark.parametrize("provider_name", sorted(DRIVERS))
def test_a_rate_limit_longer_than_the_budget_is_not_waited_out(clock, provider_name):
    wire = DRIVERS[provider_name]([("status", 429, {"Retry-After": "9000"})], clock)
    assert wire.calls == 1
    assert clock.slept == 0.0


@pytest.mark.parametrize(
    "provider_name, per_attempt",
    [("ollama", 240.0), ("anthropic", 120.0)],
)
def test_a_timeout_cannot_start_a_second_full_budget(clock, provider_name, per_attempt):
    """The F12-06 shape itself. Each attempt burns its whole per-attempt timeout; the operation
    must stop as soon as another full attempt no longer fits in 300s, instead of stacking them
    into the 394s and 617s operations Phase 17 logged."""
    wire = DRIVERS[provider_name]([("timeout", per_attempt)], clock)
    elapsed = clock.now - 1_000.0
    assert elapsed <= settings.ai_total_budget_seconds, (
        f"{provider_name} ran {elapsed:.1f}s, over the {settings.ai_total_budget_seconds}s budget"
    )
    assert wire.calls <= MAX_PROVIDER_CALLS
    # Ollama's 240s attempt leaves 60s, so there is no retry at all; Anthropic's 120s attempt
    # leaves 180s, so exactly one retry fits and then the budget closes.
    assert wire.calls == (1 if per_attempt * 2 > settings.ai_total_budget_seconds else 2)


@pytest.mark.parametrize("provider_name", sorted(DRIVERS))
def test_a_schema_correction_cannot_start_once_the_budget_is_spent(clock, provider_name):
    """The correction shares the operation's deadline, so a slow first attempt that comes back
    schema-invalid does not buy a second full budget to correct itself in."""
    wire = DRIVERS[provider_name](
        ["bad_schema", "bad_schema"], clock, total_seconds=settings.ai_total_budget_seconds
    )
    assert wire.calls == 2
    assert wire.calls <= MAX_PROVIDER_CALLS


@pytest.mark.parametrize("provider_name", sorted(DRIVERS))
def test_every_attempt_is_given_a_timeout(clock, provider_name):
    """An attempt with no timeout is an unbounded attempt, whatever the budget says."""
    wire = DRIVERS[provider_name](["network", "ok"], clock)
    assert len(wire.timeouts) == 2
    assert all(t is not None and t > 0 for t in wire.timeouts), wire.timeouts
    assert all(t <= settings.ai_total_budget_seconds for t in wire.timeouts)


# --------------------------------------------------------------------------------------
# each provider's classification of real exception objects
# --------------------------------------------------------------------------------------


def _status_error(code, headers=None):
    request = httpx.Request("POST", "http://x")
    response = httpx.Response(code, request=request, headers=headers or {})
    return httpx.HTTPStatusError(str(code), request=request, response=response)


@pytest.mark.parametrize(
    "exc, category, retry_after",
    [
        (_status_error(429), FailureCategory.RATE_LIMIT, None),
        (_status_error(429, {"Retry-After": "12"}), FailureCategory.RATE_LIMIT, 12.0),
        (_status_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
         FailureCategory.RATE_LIMIT, None),
        (_status_error(500), FailureCategory.SERVER_ERROR, None),
        (_status_error(503), FailureCategory.SERVER_ERROR, None),
        (_status_error(401), FailureCategory.AUTH, None),
        (_status_error(403), FailureCategory.AUTH, None),
        (_status_error(400), FailureCategory.CLIENT_ERROR, None),
        (_status_error(404), FailureCategory.CLIENT_ERROR, None),
        (httpx.ReadTimeout("read"), FailureCategory.TIMEOUT, None),
        (httpx.ConnectTimeout("connect"), FailureCategory.TIMEOUT, None),
        (httpx.ConnectError("refused"), FailureCategory.TRANSIENT_NETWORK, None),
        (httpx.ReadError("reset"), FailureCategory.TRANSIENT_NETWORK, None),
        (ValueError("something else entirely"), FailureCategory.UNKNOWN, None),
    ],
)
def test_ollama_failure_classification(exc, category, retry_after):
    verdict = _classify_ollama_failure(exc)
    assert verdict.category is category
    assert verdict.retry_after_seconds == retry_after
    assert verdict.retryable == (category in RETRYABLE_CATEGORIES)


def _anthropic_status_error(cls, code, headers=None):
    request = httpx.Request("POST", "http://x")
    response = httpx.Response(code, request=request, headers=headers or {},
                              json={"error": {"type": "x", "message": "y"}})
    return cls("boom", response=response, body=None)


@pytest.mark.parametrize(
    "exc, category, retry_after",
    [
        (_anthropic_status_error(anthropic.RateLimitError, 429), FailureCategory.RATE_LIMIT, None),
        (_anthropic_status_error(anthropic.RateLimitError, 429, {"Retry-After": "7"}),
         FailureCategory.RATE_LIMIT, 7.0),
        (_anthropic_status_error(anthropic.InternalServerError, 500),
         FailureCategory.SERVER_ERROR, None),
        (_anthropic_status_error(anthropic.AuthenticationError, 401), FailureCategory.AUTH, None),
        (_anthropic_status_error(anthropic.PermissionDeniedError, 403), FailureCategory.AUTH, None),
        (_anthropic_status_error(anthropic.BadRequestError, 400), FailureCategory.CLIENT_ERROR, None),
        (_anthropic_status_error(anthropic.NotFoundError, 404), FailureCategory.CLIENT_ERROR, None),
        (anthropic.APITimeoutError(request=httpx.Request("POST", "http://x")),
         FailureCategory.TIMEOUT, None),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "http://x")),
         FailureCategory.TRANSIENT_NETWORK, None),
        (ValueError("something else entirely"), FailureCategory.UNKNOWN, None),
    ],
)
def test_anthropic_failure_classification(exc, category, retry_after):
    verdict = _classify_anthropic_failure(exc)
    assert verdict.category is category
    assert verdict.retry_after_seconds == retry_after
    assert verdict.retryable == (category in RETRYABLE_CATEGORIES)


def test_an_api_response_validation_error_is_not_retried():
    """A 200 whose body the SDK could not parse is deterministic -- retrying reproduces it. It
    derives from APIError, not APIStatusError, so it reaches the classifier's default branch;
    that branch defaulting to non-retryable is what keeps a new SDK exception class from
    silently becoming a retry storm."""
    exc = anthropic.APIResponseValidationError(
        response=httpx.Response(200, request=httpx.Request("POST", "http://x"), json={"nope": 1}),
        body=None,
    )
    assert _classify_anthropic_failure(exc).category is FailureCategory.UNKNOWN
    assert not _classify_anthropic_failure(exc).retryable


# --------------------------------------------------------------------------------------
# the fallback the whole app depends on
# --------------------------------------------------------------------------------------


def test_the_no_ai_fallback_still_fires_when_the_budget_is_exhausted():
    """End to end through a real service: an exhausted budget produces the same deterministic
    template text as any other AI failure. This is why no service-layer file needed changing."""
    from app.services.career_path.ai_generation import (
        ExplainerSkillInput,
        generate_skill_explanations,
    )

    class ExhaustedProvider:
        def generate_structured(self, prompt, schema):
            raise AIBudgetExhausted("AI operation budget exhausted (deadline) after 2 calls")

        def generate_text(self, prompt):
            raise AIBudgetExhausted("AI operation budget exhausted (deadline) after 2 calls")

    items = [ExplainerSkillInput(skill_key="sql", title="SQL", phase_key="foundations", level="core")]
    explanations = generate_skill_explanations("Data Analyst", ExhaustedProvider(), items)

    assert explanations["sql"] == (
        "Builds your SQL skills -- a core part of becoming job-ready as a Data Analyst."
    )
