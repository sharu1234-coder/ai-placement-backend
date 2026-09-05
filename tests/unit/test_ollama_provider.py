import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.ai.ollama_provider import OllamaProvider, _redact_validation_error
from app.ai.provider import AIGenerationError


class Animal(BaseModel):
    name: str
    legs: int


class ScoreSchema(BaseModel):
    """Shaped like this app's real AI-score schemas (AnswerGrade.score, ...) -- used only to
    simulate a validation failure whose invalid `input_value` carries sensitive-looking
    content, for the redaction tests below."""

    score: float


def _fake_response(body: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code, json={"response": body}, request=httpx.Request("POST", "http://x")
    )


def _error_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "http://x"))


def _counting_post(responses):
    """A stand-in for httpx.post that returns each item from `responses` in order (raising it
    immediately if it's an Exception instance) and records how many times it was called --
    used to prove the retry path actually re-invokes the network call, not just that the
    final outcome is correct."""
    calls = {"count": 0}
    iterator = iter(responses)

    def _post(*args, **kwargs):
        calls["count"] += 1
        item = next(iterator)
        if isinstance(item, Exception):
            raise item
        return item

    return _post, calls


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
    """A sustained network outage (every attempt transient) still exhausts its bounded
    retries and eventually raises AIGenerationError -- 1 initial attempt + 2 retries = 3
    total calls, not an infinite retry loop."""
    post, calls = _counting_post([httpx.ConnectError("connection refused")] * 3)
    monkeypatch.setattr(httpx, "post", post)
    provider = OllamaProvider(base_url="http://fake", model="fake-model")
    with pytest.raises(AIGenerationError):
        provider.generate_text("hello")
    assert calls["count"] == 3


def test_transient_network_error_is_retried_then_succeeds(monkeypatch):
    """Real gap, now fixed: a single transient connection error used to be treated
    identically to a permanent failure -- surfaced immediately as AIGenerationError with
    zero retries. It must now get a bounded retry and succeed once the blip clears."""
    post, calls = _counting_post(
        [httpx.ConnectError("connection refused"), _fake_response('{"name": "dog", "legs": 4}')]
    )
    monkeypatch.setattr(httpx, "post", post)
    provider = OllamaProvider(base_url="http://fake", model="fake-model")

    result = provider.generate_structured("describe a dog", Animal)

    assert result == Animal(name="dog", legs=4)
    assert calls["count"] == 2


def test_transient_rate_limit_response_is_retried_then_succeeds(monkeypatch):
    post, calls = _counting_post([_error_response(429), _fake_response('{"name": "dog", "legs": 4}')])
    monkeypatch.setattr(httpx, "post", post)
    provider = OllamaProvider(base_url="http://fake", model="fake-model")

    result = provider.generate_structured("describe a dog", Animal)

    assert result == Animal(name="dog", legs=4)
    assert calls["count"] == 2


def test_transient_server_error_response_is_retried_then_succeeds(monkeypatch):
    post, calls = _counting_post([_error_response(503), _fake_response('{"name": "dog", "legs": 4}')])
    monkeypatch.setattr(httpx, "post", post)
    provider = OllamaProvider(base_url="http://fake", model="fake-model")

    result = provider.generate_structured("describe a dog", Animal)

    assert result == Animal(name="dog", legs=4)
    assert calls["count"] == 2


def test_permanent_client_error_response_is_not_retried(monkeypatch):
    """A genuine 4xx (e.g. a malformed request) cannot succeed on an unchanged retry -- it
    must surface as AIGenerationError after exactly one call, not be retried pointlessly."""
    post, calls = _counting_post([_error_response(400), _fake_response('{"name": "dog", "legs": 4}')])
    monkeypatch.setattr(httpx, "post", post)
    provider = OllamaProvider(base_url="http://fake", model="fake-model")

    with pytest.raises(AIGenerationError):
        provider.generate_structured("describe a dog", Animal)

    assert calls["count"] == 1


def test_malformed_response_shape_is_not_retried(monkeypatch):
    """A 200 response with an unexpected body shape (see the KeyError/ValueError handling in
    _call_ollama) is a permanent, not transient, failure -- retrying an unchanged malformed
    body cannot help, so it must not be retried either."""
    post, calls = _counting_post(
        [
            httpx.Response(status_code=200, json={"error": "model not found"}, request=httpx.Request("POST", "http://x")),
            _fake_response('{"name": "dog", "legs": 4}'),
        ]
    )
    monkeypatch.setattr(httpx, "post", post)
    provider = OllamaProvider(base_url="http://fake", model="fake-model")

    with pytest.raises(AIGenerationError):
        provider.generate_text("hello")

    assert calls["count"] == 1


def test_validation_failure_log_does_not_include_the_raw_invalid_value(monkeypatch, caplog):
    """Real audit gap, now fixed: ValidationError.__str__() includes the actual invalid
    input_value -- for this app's real schemas that can be AI-generated text ultimately
    derived from a student's resume/JD/interview answers. The logged warning must carry a
    redacted field-path/error-type summary, never the raw value."""
    secret = "RESUME_SECRET_a-very-specific-sensitive-token"
    responses = iter([_fake_response(f'{{"score": "{secret}"}}'), _fake_response('{"score": 0.5}')])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: next(responses))
    provider = OllamaProvider(base_url="http://fake", model="fake-model")

    with caplog.at_level("WARNING"):
        provider.generate_structured("grade this", ScoreSchema)

    assert secret not in caplog.text
    assert "field error(s)" in caplog.text


def test_redact_validation_error_never_includes_the_raw_invalid_value():
    secret = "RESUME_SECRET_a-very-specific-sensitive-token"
    try:
        ScoreSchema.model_validate({"score": secret})
        raise AssertionError("expected model_validate(...) to raise")
    except ValidationError as error:
        summary = _redact_validation_error(error)

    assert secret not in summary
    assert "score" in summary
    assert "field error(s)" in summary


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
