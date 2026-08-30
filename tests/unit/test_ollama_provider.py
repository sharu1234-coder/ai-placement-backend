import httpx
import pytest
from pydantic import BaseModel

from app.ai.ollama_provider import OllamaProvider
from app.ai.provider import AIGenerationError


class Animal(BaseModel):
    name: str
    legs: int


def _fake_response(body: str) -> httpx.Response:
    return httpx.Response(status_code=200, json={"response": body}, request=httpx.Request("POST", "http://x"))


def test_generate_structured_succeeds_first_try(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _fake_response('{"name": "dog", "legs": 4}')
    )
    provider = OllamaProvider(base_url="http://fake", model="fake-model")
    result = provider.generate_structured("describe a dog", Animal)
    assert result == Animal(name="dog", legs=4)


def test_generate_structured_retries_then_succeeds(monkeypatch):
    responses = iter(
        [
            _fake_response('{"name": "dog"}'),  # missing "legs" -> validation error
            _fake_response('{"name": "dog", "legs": 4}'),  # corrected
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: next(responses))
    provider = OllamaProvider(base_url="http://fake", model="fake-model")
    result = provider.generate_structured("describe a dog", Animal)
    assert result == Animal(name="dog", legs=4)


def test_generate_structured_raises_after_two_failures(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_response('{"name": "dog"}'))
    provider = OllamaProvider(base_url="http://fake", model="fake-model")
    with pytest.raises(AIGenerationError):
        provider.generate_structured("describe a dog", Animal)


def test_network_error_raises_ai_generation_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _raise)
    provider = OllamaProvider(base_url="http://fake", model="fake-model")
    with pytest.raises(AIGenerationError):
        provider.generate_text("hello")


def test_unexpected_response_shape_raises_ai_generation_error_not_key_error(monkeypatch):
    """Real bug, found via reproduction: a 200 response missing the expected "response" key
    (e.g. Ollama returning {"error": "model not found"} instead) previously raised a bare
    KeyError, bypassing every `except AIGenerationError` handler in the API layer and
    surfacing as an unhandled 500 instead of the intended, caught error path."""
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: httpx.Response(
            status_code=200, json={"error": "model not found"}, request=httpx.Request("POST", "http://x")
        ),
    )
    provider = OllamaProvider(base_url="http://fake", model="fake-model")
    with pytest.raises(AIGenerationError):
        provider.generate_text("hello")


def test_non_json_response_body_raises_ai_generation_error_not_json_decode_error(monkeypatch):
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: httpx.Response(status_code=200, content=b"not json at all", request=httpx.Request("POST", "http://x")),
    )
    provider = OllamaProvider(base_url="http://fake", model="fake-model")
    with pytest.raises(AIGenerationError):
        provider.generate_text("hello")
