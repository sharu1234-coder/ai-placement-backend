from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.ai.anthropic_provider import AnthropicProvider, _redact_validation_error
from app.ai.provider import AIGenerationError
from app.core.config import settings


class SampleSchema(BaseModel):
    answer: str


class ScoreSchema(BaseModel):
    """A second schema shaped like this app's real AI-score schemas (AnswerGrade.score,
    GeneratedInterviewEvaluation's fields, ...) -- used only to simulate a validation failure
    whose invalid `input_value` itself carries sensitive-looking content, for the redaction
    test below."""

    score: float


def _validation_error() -> ValidationError:
    try:
        SampleSchema.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected model_validate({}) to raise")


def _validation_error_with_sensitive_value(secret: str) -> ValidationError:
    try:
        ScoreSchema.model_validate({"score": secret})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected model_validate(...) to raise")


def _api_status_error(status_code: int = 429) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return anthropic.APIStatusError("rate limited", response=response, body=None)


def _api_connection_error() -> anthropic.APIConnectionError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=request)


@pytest.fixture()
def provider() -> AnthropicProvider:
    instance = AnthropicProvider(api_key="test-key", model="claude-sonnet-5")
    instance._client = MagicMock()
    return instance


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    with pytest.raises(AIGenerationError):
        AnthropicProvider(api_key=None)


def test_generate_structured_returns_parsed_output(provider):
    provider._client.messages.parse.return_value = SimpleNamespace(
        parsed_output=SampleSchema(answer="Paris")
    )

    result = provider.generate_structured("What is the capital of France?", SampleSchema)

    assert result == SampleSchema(answer="Paris")
    provider._client.messages.parse.assert_called_once()


def test_generate_structured_retries_once_then_succeeds(provider):
    provider._client.messages.parse.side_effect = [
        _validation_error(),
        SimpleNamespace(parsed_output=SampleSchema(answer="Paris")),
    ]

    result = provider.generate_structured("prompt", SampleSchema)

    assert result == SampleSchema(answer="Paris")
    assert provider._client.messages.parse.call_count == 2


def test_generate_structured_raises_after_two_validation_failures(provider):
    provider._client.messages.parse.side_effect = [_validation_error(), _validation_error()]

    with pytest.raises(AIGenerationError):
        provider.generate_structured("prompt", SampleSchema)

    assert provider._client.messages.parse.call_count == 2


def test_generate_structured_raises_on_missing_parsed_output(provider):
    provider._client.messages.parse.return_value = SimpleNamespace(parsed_output=None)

    with pytest.raises(AIGenerationError):
        provider.generate_structured("prompt", SampleSchema)


def test_generate_structured_permanent_status_error_is_not_retried(provider):
    """A genuine 4xx client error (bad auth, malformed request) cannot succeed on an
    unchanged retry -- it isn't a schema problem either, so it must not be retried by the
    schema-correction path OR the transient-failure retry added for 429/5xx/network errors."""
    provider._client.messages.parse.side_effect = _api_status_error(400)

    with pytest.raises(AIGenerationError):
        provider.generate_structured("prompt", SampleSchema)

    provider._client.messages.parse.assert_called_once()


def test_generate_structured_transient_rate_limit_is_retried_then_succeeds(provider):
    """Real gap, now fixed: a single transient 429 used to be treated identically to a
    permanent failure -- immediately surfaced as AIGenerationError with zero retries. It
    must now get a bounded retry and succeed once the transient condition clears."""
    provider._client.messages.parse.side_effect = [
        _api_status_error(429),
        SimpleNamespace(parsed_output=SampleSchema(answer="Paris")),
    ]

    result = provider.generate_structured("What is the capital of France?", SampleSchema)

    assert result == SampleSchema(answer="Paris")
    assert provider._client.messages.parse.call_count == 2


def test_generate_structured_transient_server_error_is_retried_then_succeeds(provider):
    provider._client.messages.parse.side_effect = [
        _api_status_error(503),
        SimpleNamespace(parsed_output=SampleSchema(answer="Paris")),
    ]

    result = provider.generate_structured("prompt", SampleSchema)

    assert result == SampleSchema(answer="Paris")
    assert provider._client.messages.parse.call_count == 2


def test_generate_structured_connection_error_is_retried_then_succeeds(provider):
    provider._client.messages.parse.side_effect = [
        _api_connection_error(),
        SimpleNamespace(parsed_output=SampleSchema(answer="Paris")),
    ]

    result = provider.generate_structured("prompt", SampleSchema)

    assert result == SampleSchema(answer="Paris")
    assert provider._client.messages.parse.call_count == 2


def test_generate_structured_exhausts_transient_retries_then_raises(provider):
    """Bounded, not infinite: a sustained outage (every attempt transient) must still
    eventually surface as AIGenerationError rather than retrying forever."""
    provider._client.messages.parse.side_effect = [
        _api_status_error(503), _api_status_error(503), _api_status_error(503),
    ]

    with pytest.raises(AIGenerationError):
        provider.generate_structured("prompt", SampleSchema)

    # 1 initial attempt + 2 retries = 3 total calls, then it gives up.
    assert provider._client.messages.parse.call_count == 3


def test_redact_validation_error_never_includes_the_raw_invalid_value():
    secret = "RESUME_SECRET_a-very-specific-sensitive-token"
    error = _validation_error_with_sensitive_value(secret)

    summary = _redact_validation_error(error)

    assert secret not in summary
    assert "score" in summary  # field path is safe to keep -- it's schema shape, not content
    assert "field error(s)" in summary


def test_validation_failure_log_does_not_include_the_raw_invalid_value(provider, caplog):
    """Real audit gap, now fixed: ValidationError.__str__() includes the actual invalid
    input_value -- for this app's real schemas (AnswerGrade.score, interview evaluation
    dimensions, ...) that can be AI-generated text ultimately derived from a student's
    resume/JD/interview answers. The logged warning must carry a redacted field-path/
    error-type summary, never the raw value, even though the raised AIGenerationError's own
    message (never logged by this call) is allowed to keep the full detail for a caller that
    explicitly wants it."""
    secret = "RESUME_SECRET_a-very-specific-sensitive-token"
    provider._client.messages.parse.side_effect = [
        _validation_error_with_sensitive_value(secret),
        SimpleNamespace(parsed_output=SampleSchema(answer="ok")),
    ]

    with caplog.at_level("WARNING"):
        provider.generate_structured("prompt", SampleSchema)

    assert secret not in caplog.text
    assert "field error(s)" in caplog.text


def test_generate_text_joins_text_blocks_only(provider):
    provider._client.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="reasoning..."),
            SimpleNamespace(type="text", text="Hello, "),
            SimpleNamespace(type="text", text="world."),
        ]
    )

    result = provider.generate_text("Say hello")

    assert result == "Hello, world."


def test_generate_text_raises_on_api_error(provider):
    provider._client.messages.create.side_effect = _api_status_error(500)

    with pytest.raises(AIGenerationError):
        provider.generate_text("prompt")
