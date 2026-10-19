import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

import httpx
from pydantic import ValidationError

from app.ai.provider import AICapability, AICapabilityError, AIGenerationError, AIProvider, T
from app.ai.retry import AIOperationBudget, FailureCategory, FailureVerdict
from app.core.config import settings

logger = logging.getLogger(__name__)


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


def _post_and_raise(base_url: str, payload: dict, timeout: float) -> httpx.Response:
    """Split out from _call_ollama so the budget retries exactly this (the network call +
    status check), not the JSON-shape parsing that follows it -- a malformed-but-200 body must
    never be retried, only a real network/status failure."""
    response = httpx.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
    response.raise_for_status()
    return response


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    """A server's own `Retry-After`, in seconds, when it sent a usable one. Only the delta-
    seconds form is honoured; the HTTP-date form is ignored rather than guessed at, which
    simply leaves the budget's own fixed backoff in charge."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError):
        return None


def _classify_ollama_failure(exc: Exception) -> FailureVerdict:
    """Which of the failure categories this is, and therefore whether repeating the identical
    request could possibly help.

    Retryable: a timeout, a network-level failure with no response at all (connection refused,
    reset, ...), an HTTP 429, or a 5xx. Not retryable: 401/403 (no key change can come from a
    retry), any other 4xx (an unchanged request cannot fix a malformed request or a missing
    model), and anything unrecognised -- unknown failures default to *not* retrying, so a new
    exception class can never silently become a retry storm.

    Body-shape parsing deliberately happens outside the budget entirely, so a malformed 200 can
    never even reach this classifier."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return FailureVerdict(FailureCategory.RATE_LIMIT, _retry_after_seconds(exc.response))
        if status >= 500:
            return FailureVerdict(FailureCategory.SERVER_ERROR)
        if status in (401, 403):
            return FailureVerdict(FailureCategory.AUTH)
        return FailureVerdict(FailureCategory.CLIENT_ERROR)
    # TimeoutException is a TransportError is an HTTPError, so it has to be tested first.
    if isinstance(exc, httpx.TimeoutException):
        return FailureVerdict(FailureCategory.TIMEOUT)
    if isinstance(exc, httpx.HTTPError):
        return FailureVerdict(FailureCategory.TRANSIENT_NETWORK)
    return FailureVerdict(FailureCategory.UNKNOWN)


def _extract_response_text(body: object) -> str:
    """Pull Ollama's `response` string out of an already-parsed 200 body, raising
    AIGenerationError for every shape that isn't `{"response": "<text>"}`.

    Split out from _call_ollama because the previous inline `response.json()["response"]`
    only had its KeyError/ValueError paths covered. Re-verified by reproduction, not assumed:
    a 200 whose JSON body is a *list*, a bare string, or literal `null` (all things a
    misconfigured reverse proxy or a non-Ollama service on OLLAMA_BASE_URL can return) raises
    TypeError -- `list indices must be integers`, `'NoneType' object is not subscriptable` --
    which is neither a KeyError nor a ValueError, so it sailed straight past the handler that
    was added for exactly this class of bug and surfaced as an unhandled 500. A 200 of
    `{"response": null}` is the same failure a step later: it returned None from a function
    annotated `-> str`, leaving every caller to fail on it further downstream instead of here.
    """
    if not isinstance(body, dict):
        raise AIGenerationError(
            f"Ollama returned an unexpected response shape: expected a JSON object, got {type(body).__name__}"
        )
    text = body.get("response")
    if not isinstance(text, str):
        raise AIGenerationError(
            "Ollama returned an unexpected response shape: 'response' was "
            f"{type(text).__name__}, expected a string"
        )
    return text


# Module-level cache: the Whisper model is expensive to load (~seconds, plus a one-time
# download), and OllamaProvider is constructed fresh per request by get_ai_provider(), so
# this must live outside the instance or every voice-answer request would reload it.
# Guarded by _whisper_model_lock -- audio answers run via run_in_threadpool (see
# interviews.py), so two concurrent first requests could otherwise both see None and both
# construct a WhisperModel.
_whisper_model = None
_whisper_model_lock = threading.Lock()

# A WAV file is a 44-byte canonical header plus samples, so anything at or under that is a
# header with no audio in it -- which is what a driver that "succeeded" without synthesising
# anything leaves behind.
_MIN_WAV_BYTES = 45


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    with _whisper_model_lock:
        if _whisper_model is None:
            # The xet-accelerated download path in huggingface_hub hung indefinitely in
            # dev testing; the plain HTTPS fallback works reliably. Must be set before the
            # first huggingface_hub/faster_whisper import touches it.
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            from faster_whisper import WhisperModel

            logger.info("Loading Whisper model '%s' (first use only)...", settings.whisper_model_size)
            _whisper_model = WhisperModel(settings.whisper_model_size, device="cpu", compute_type="int8")
    return _whisper_model


def _select_voice(engine) -> Optional[str]:
    """Point the local speech driver at a voice matching the configured preference.

    Thin wrapper over `app.core.voice_selection.select_voice`, kept because this module's
    call site reads better with a one-argument helper and because the name appears in
    existing tests.

    The logic used to live here in full, and that was the defect. This was the *only* place
    that selected a voice, and it is on the AI-provider path -- reached by the Mock Interview
    question-speaking call in `app/services/interview_service.py`. The Voice Career Coach runs
    through `app/voice/turn_runtime.py` -> `app/speech/pyttsx3_tts.py`, which had no selection
    at all, so it kept speaking in the host default. Both paths now share one implementation.

    Returns the chosen voice's name for the caller to record, or None when the host default
    was kept. The full typed outcome is logged by `select_voice`; a caller that needs to act
    on *why* a preference went unmet should call it directly rather than reading this None.
    """
    from app.core.voice_selection import log_choice, select_voice

    choice = select_voice(engine)
    log_choice(choice, where="ollama synthesize_speech")
    return choice.voice_name


class OllamaProvider(AIProvider):
    # Capabilities are derived from the methods below (see AIProvider.__init_subclass__):
    # transcribe() via faster-whisper, synthesize_speech() via pyttsx3. Whether the TTS
    # driver is actually usable on the host is a separate, runtime question -- see
    # synthesize_speech below, which raises AICapabilityError with the reason rather than
    # letting a missing OS package surface as a generic failure.
    name = "ollama"

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None) -> None:
        self.base_url = base_url or settings.ollama_base_url
        self.model = model or settings.ollama_model

    def _budget(self, operation: str) -> AIOperationBudget:
        """One budget per logical AI operation. The schema's own name is the operation name, so
        every one of the app's AI call sites becomes individually visible in the logs without
        any of them having to pass anything new."""
        return AIOperationBudget(
            operation=operation,
            provider="ollama",
            per_attempt_seconds=settings.ollama_timeout_seconds,
        )

    def generate_structured(self, prompt: str, schema: type[T]) -> T:
        schema_json = schema.model_json_schema()
        # One budget, both stages. The schema correction below therefore draws down the same
        # call cap and the same deadline as the initial attempt instead of starting a second
        # independent budget -- which is the specific multiplication F12-06 was about.
        with self._budget(schema.__name__) as budget:
            raw = self._call_ollama(prompt, schema_json, budget, stage="initial")
            try:
                return schema.model_validate_json(raw)
            except ValidationError as first_error:
                logger.warning(
                    "AI output failed schema validation, retrying once: %s", _redact_validation_error(first_error)
                )
                correction_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response did not match the required schema. "
                    f"Validation errors: {first_error}\n"
                    "Return ONLY valid JSON matching the schema, with no extra commentary."
                )
                raw_retry = self._call_ollama(correction_prompt, schema_json, budget, stage="correction")
                try:
                    return schema.model_validate_json(raw_retry)
                except ValidationError as second_error:
                    budget.record(FailureCategory.SCHEMA)
                    raise AIGenerationError(
                        f"AI output failed schema validation twice: {_redact_validation_error(second_error)}"
                    ) from second_error

    def generate_text(self, prompt: str) -> str:
        with self._budget("text") as budget:
            return self._call_ollama(prompt, None, budget, stage="initial")

    def transcribe(self, audio: bytes) -> str:
        """Dev-only local STT (faster-whisper, CPU). Swap for a cloud STT provider before
        real user testing -- accuracy/latency are not production-grade."""
        model = _get_whisper_model()
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name
        try:
            segments, _ = model.transcribe(tmp_path, language="en")
            return " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            raise AIGenerationError(f"Could not transcribe audio: {exc}") from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def synthesize_speech(self, text: str) -> bytes:
        """Dev-only local TTS via pyttsx3. Swap for a cloud TTS provider before real user
        testing -- voice quality is robotic, not production-grade.

        `pyttsx3.init()` is called without a driverName, so it selects the platform default:
        sapi5 on Windows, nsss on macOS, espeak on everything else including Linux. The code
        is not Windows-specific; the *host* is what decides whether this works. The Linux
        espeak driver loads libespeak through ctypes, so the espeak-ng OS package must be
        installed -- pip cannot supply it, and requirements.txt therefore cannot express it.
        See README "Server-side TTS (eSpeak on Linux)".

        An unusable driver is reported as AICapabilityError, not as a generic failure: the
        two need different responses (install a package / investigate the request), and only
        the typed error says which. VERIFIED on Windows; the Linux path is NOT YET TESTED --
        tests/unit/test_tts_linux_contract.py holds the test that resolves it.
        """
        import pyttsx3

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            try:
                engine = pyttsx3.init()
            except Exception as exc:
                # The driver could not be loaded at all. On Linux this is what a missing
                # espeak-ng looks like; pyttsx3 surfaces it as an OSError from ctypes or as
                # its own EngineError, so the type is not reliable enough to match on.
                raise AICapabilityError(
                    self.name,
                    AICapability.SYNTHESIZE,
                    reason=f"the local text-to-speech driver could not be initialised: {exc}",
                ) from exc

            _select_voice(engine)
            engine.save_to_file(text, tmp_path)
            engine.runAndWait()
            audio = Path(tmp_path).read_bytes()
        except AICapabilityError:
            raise
        except Exception as exc:
            raise AIGenerationError(f"Could not synthesize speech: {exc}") from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        # A driver that loads but writes nothing is the other half of the same problem: the
        # espeak driver's save_to_file support is not documented per-driver, so an empty or
        # header-only file must not be returned to the client as if it were audio. Android's
        # MediaPlayer would fail to prepare it and the student would see a silent failure.
        if len(audio) < _MIN_WAV_BYTES or audio[:4] != b"RIFF":
            raise AICapabilityError(
                self.name,
                AICapability.SYNTHESIZE,
                reason=(
                    "the local text-to-speech driver produced no usable WAV output "
                    f"({len(audio)} bytes)"
                ),
            )
        return audio

    def _call_ollama(
        self, prompt: str, format_schema: Optional[dict], budget: AIOperationBudget, *, stage: str
    ) -> str:
        payload: dict = {"model": self.model, "prompt": prompt, "stream": False}
        if format_schema is not None:
            payload["format"] = format_schema

        # budget.call wraps only the raw HTTP call + status check: a genuinely transient failure
        # (connection error, timeout, 429, 5xx) gets at most one bounded retry, and only if the
        # retry's full timeout still fits in what the operation has left. A permanent 4xx, or an
        # unexpected-but-200 body (parsed below, outside the budget), is never retried. The
        # timeout comes from the budget rather than straight from settings, because it is clamped
        # to the remaining wall clock -- that clamp is half of how the deadline is enforced.
        try:
            response = budget.call(
                lambda timeout: _post_and_raise(self.base_url, payload, timeout),
                classify=_classify_ollama_failure,
                stage=stage,
            )
            body = response.json()
        except httpx.HTTPError as exc:
            raise AIGenerationError(f"Could not reach Ollama at {self.base_url}: {exc}") from exc
        except ValueError as exc:
            # A 200 whose body isn't valid JSON at all -- response.json() raises a ValueError
            # subclass (json.JSONDecodeError). Found via a real repro, not assumed: this
            # previously escaped as a bare JSONDecodeError, bypassing every
            # `except AIGenerationError` handler in the API layer and surfacing as an
            # unhandled 500 instead of the intended error path.
            raise AIGenerationError(f"Ollama returned a non-JSON response body: {exc}") from exc

        # Every remaining unexpected-but-200 shape (wrong container type, missing key, null
        # value) is handled here -- see _extract_response_text for the specific shapes and why
        # the previous KeyError/ValueError-only handling was not enough.
        return _extract_response_text(body)
