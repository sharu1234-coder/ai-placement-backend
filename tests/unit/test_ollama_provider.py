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
