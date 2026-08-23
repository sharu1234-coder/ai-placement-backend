from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.ai.anthropic_provider import AnthropicProvider
from app.ai.provider import AIGenerationError
from app.core.config import settings


class SampleSchema(BaseModel):
    answer: str


def _validation_error() -> ValidationError:
    try:
        SampleSchema.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected model_validate({}) to raise")


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


def test_generate_structured_api_status_error_not_retried(provider):
    provider._client.messages.parse.side_effect = _api_status_error()

    with pytest.raises(AIGenerationError):
        provider.generate_structured("prompt", SampleSchema)

    # A network/rate-limit failure isn't a schema problem -- retrying with a "fix your
    # schema" correction prompt wouldn't help, so it must not be retried.
    provider._client.messages.parse.assert_called_once()


def test_generate_structured_api_connection_error_not_retried(provider):
    provider._client.messages.parse.side_effect = _api_connection_error()

    with pytest.raises(AIGenerationError):
        provider.generate_structured("prompt", SampleSchema)

    provider._client.messages.parse.assert_called_once()


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
