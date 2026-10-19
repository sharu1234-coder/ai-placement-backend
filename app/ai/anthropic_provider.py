import logging
from typing import Optional

import anthropic
from pydantic import ValidationError

from app.ai.provider import AIGenerationError, AIProvider, T
from app.ai.retry import AIOperationBudget, FailureCategory, FailureVerdict
from app.core.config import settings

logger = logging.getLogger(__name__)

# All of this app's structured schemas (questions, grades, JD parses, interview
# evaluations, roadmap task lists) comfortably fit well under this; sized generously per
# "don't lowball max_tokens" rather than tuned to the smallest schema.
_MAX_TOKENS = 16000


def _redact_validation_error(error: ValidationError) -> str:
    """A short, safe summary of a Pydantic ValidationError -- error count, field path, and
    error type per field, deliberately never the error's own str()/repr(). Real gap:
    ValidationError.__str__() includes each field's actual `input_value`, which for this
    app's schemas can be an excerpt of AI-generated text ultimately derived from a student's
    resume/JD/interview answers -- logging it verbatim would put that content into
    application logs on every validation failure."""
    field_summaries = [
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'} ({e['type']})" for e in error.errors()
    ]
    return f"{len(field_summaries)} field error(s): {', '.join(field_summaries)}"


def _retry_after_seconds(exc: anthropic.APIStatusError) -> Optional[float]:
    """The server's own `Retry-After`, in seconds, when it sent a usable one.

    This has to live here now. The SDK's own retry loop is disabled (max_retries=0, see
    __init__) so that the application layer is the single authority on retries -- and the SDK's
    loop was the only thing in the AI path that honoured this header. Dropping it without
    replacing it would have turned correct rate-limit behaviour into a regression. Only the
    delta-seconds form is honoured; the HTTP-date form is ignored rather than guessed at, which
    leaves the budget's own fixed backoff in charge."""
    raw = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _classify_anthropic_failure(exc: Exception) -> FailureVerdict:
    """Which of the failure categories this is, and therefore whether repeating the identical
    request could possibly help.

    Retryable: a timeout, a network-level failure, an HTTP 429, or a 5xx. Not retryable:
    401/403 (no retry produces a different API key), any other 4xx (an unchanged request cannot
    fix a malformed request or an unsupported model), APIResponseValidationError (a 200 whose
    body the SDK could not parse -- deterministic), and anything unrecognised, so that a new SDK
    exception class can never silently become a retry storm."""
    # APITimeoutError derives from APIConnectionError, so it has to be tested first.
    if isinstance(exc, anthropic.APITimeoutError):
        return FailureVerdict(FailureCategory.TIMEOUT)
    if isinstance(exc, anthropic.APIConnectionError):
        return FailureVerdict(FailureCategory.TRANSIENT_NETWORK)
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 429:
            return FailureVerdict(FailureCategory.RATE_LIMIT, _retry_after_seconds(exc))
        if exc.status_code >= 500:
            return FailureVerdict(FailureCategory.SERVER_ERROR)
        if exc.status_code in (401, 403):
            return FailureVerdict(FailureCategory.AUTH)
        return FailureVerdict(FailureCategory.CLIENT_ERROR)
    return FailureVerdict(FailureCategory.UNKNOWN)


class AnthropicProvider(AIProvider):
    """Production cloud AI provider, selected via AI_PROVIDER=anthropic (see
    app/ai/provider.py:get_ai_provider). Requires ANTHROPIC_API_KEY in the environment --
    never hardcoded, never shipped to the Android app (Android only ever talks to this
    backend, never Anthropic directly -- see PROJECT_HANDOFF.md section 5, "AI Rules").

    Uses claude-sonnet-5 by default (ANTHROPIC_MODEL overrides) -- Anthropic's own
    guidance recommends Sonnet-tier for high-volume production workloads like this app's
    (question generation, answer grading, JD/resume parsing, interview evaluation) over
    Opus-tier's ~1.7x higher per-token cost; swap per-deployment if a different
    quality/cost tradeoff is wanted.

    transcribe()/synthesize_speech() are intentionally not implemented here -- production
    STT/TTS is separate provider work (this class covers only generate_structured/
    generate_text, i.e. text generation). That absence is now declared in `capabilities`
    rather than only discovered by calling: with AI_PROVIDER=anthropic the voice routes are
    refused while the request is still being set up (see provider.requires_ai) instead of
    failing part-way through, which is what used to happen.
    """

    name = "anthropic"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        key = api_key or settings.anthropic_api_key
        if not key:
            raise AIGenerationError(
                "ANTHROPIC_API_KEY is not set -- required when AI_PROVIDER=anthropic."
            )
        # max_retries=0 is load-bearing, not tidiness. The SDK's default is 2, i.e. 3 wire calls
        # per SDK call, which multiplied with this app's own retry and its schema correction:
        # measured on the wire at 16 provider calls and >=32 minutes for one logical operation,
        # and 9 calls plus 180.6s of sleeping for a single sustained 429. The application layer
        # is the one authority on retry policy (app/ai/retry.py), so the SDK must not add a
        # second, invisible one underneath it. Anything the SDK's loop used to do for us --
        # notably honouring Retry-After -- is now done explicitly by
        # _classify_anthropic_failure/_retry_after_seconds above.
        self._client = anthropic.Anthropic(
            api_key=key, timeout=settings.anthropic_timeout_seconds, max_retries=0
        )
        self.model = model or settings.anthropic_model

    def _budget(self, operation: str) -> AIOperationBudget:
        """One budget per logical AI operation. The schema's own name is the operation name, so
        every one of the app's AI call sites becomes individually visible in the logs without
        any of them having to pass anything new."""
        return AIOperationBudget(
            operation=operation,
            provider="anthropic",
            per_attempt_seconds=settings.anthropic_timeout_seconds,
        )

    def generate_structured(self, prompt: str, schema: type[T]) -> T:
        # One budget, both stages. The schema correction below therefore draws down the same call
        # cap and the same deadline as the initial attempt instead of starting a second
        # independent budget -- which is the specific multiplication F12-06 was about.
        with self._budget(schema.__name__) as budget:
            try:
                return self._call_parse(prompt, schema, budget, stage="initial")
            except ValidationError as first_error:
                logger.warning(
                    "AI output failed schema validation, retrying once: %s", _redact_validation_error(first_error)
                )
                correction_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response did not match the required schema. "
                    f"Validation errors: {first_error}\n"
                    "Return ONLY corrected structured data."
                )
                try:
                    return self._call_parse(correction_prompt, schema, budget, stage="correction")
                except ValidationError as second_error:
                    budget.record(FailureCategory.SCHEMA)
                    raise AIGenerationError(
                        f"AI output failed schema validation twice: {_redact_validation_error(second_error)}"
                    ) from second_error

    def _call_parse(
        self, prompt: str, schema: type[T], budget: AIOperationBudget, *, stage: str
    ) -> T:
        # output_format enforces the JSON schema server-side (same guarantee OllamaProvider
        # gets from Ollama's `format=` parameter) -- a ValidationError here means the model
        # produced text that still didn't conform, not a network/auth problem.
        #
        # budget.call wraps only the raw SDK call: a genuinely transient failure (network blip,
        # 429, 5xx) gets at most one bounded retry, and only if the retry's full timeout still
        # fits in what the operation has left -- a permanent 4xx (or the final attempt's failure)
        # raises straight through, matching the "never retry a permanent failure pointlessly"
        # rule. The per-request timeout comes from the budget rather than only from the client,
        # because it is clamped to the remaining wall clock.
        try:
            response = budget.call(
                lambda timeout: self._client.messages.parse(
                    model=self.model,
                    max_tokens=_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                    output_format=schema,
                    timeout=timeout,
                ),
                classify=_classify_anthropic_failure,
                stage=stage,
            )
        except anthropic.APIStatusError as exc:
            raise AIGenerationError(f"Anthropic API call failed ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIGenerationError(f"Could not reach the Anthropic API: {exc}") from exc
        except anthropic.AnthropicError as exc:
            # Catch-all for every remaining SDK error, verified against the installed SDK's own
            # class hierarchy rather than assumed: APIResponseValidationError derives from
            # APIError, NOT from APIStatusError or APIConnectionError, so the two handlers
            # above miss it entirely. It is raised precisely on the case this provider's
            # Ollama counterpart already handles -- an HTTP 200 whose body doesn't have the
            # shape the SDK expects -- and it previously escaped raw, past every
            # `except AIGenerationError` handler in the API layer, as an unhandled 500.
            raise AIGenerationError(f"Anthropic returned an unexpected response: {type(exc).__name__}") from exc

        if getattr(response, "parsed_output", None) is None:
            raise AIGenerationError("Anthropic response did not include structured output")
        return response.parsed_output

    def generate_text(self, prompt: str) -> str:
        try:
            with self._budget("text") as budget:
                response = budget.call(
                    lambda timeout: self._client.messages.create(
                        model=self.model,
                        max_tokens=_MAX_TOKENS,
                        messages=[{"role": "user", "content": prompt}],
                        timeout=timeout,
                    ),
                    classify=_classify_anthropic_failure,
                    stage="initial",
                )
        except anthropic.APIStatusError as exc:
            raise AIGenerationError(f"Anthropic API call failed ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIGenerationError(f"Could not reach the Anthropic API: {exc}") from exc
        except anthropic.AnthropicError as exc:
            # Same gap as in _call_parse above -- see its comment.
            raise AIGenerationError(f"Anthropic returned an unexpected response: {type(exc).__name__}") from exc

        # A 200 whose `content` is absent or null rather than a list raised a bare TypeError
        # ("'NoneType' object is not iterable") straight out of the comprehension below,
        # bypassing every `except AIGenerationError` handler -- the same unexpected-but-200
        # failure mode fixed on the Ollama side.
        content = getattr(response, "content", None)
        if not isinstance(content, list):
            raise AIGenerationError(
                "Anthropic response did not include a content list "
                f"(got {type(content).__name__})"
            )
        return "".join(block.text for block in content if getattr(block, "type", None) == "text")
