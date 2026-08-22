import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

import httpx
from pydantic import ValidationError

from app.ai.provider import AIGenerationError, AIProvider, T
from app.core.config import settings

logger = logging.getLogger(__name__)

# Module-level cache: the Whisper model is expensive to load (~seconds, plus a one-time
# download), and OllamaProvider is constructed fresh per request by get_ai_provider(), so
# this must live outside the instance or every voice-answer request would reload it.
# Guarded by _whisper_model_lock -- audio answers run via run_in_threadpool (see
# interviews.py), so two concurrent first requests could otherwise both see None and both
# construct a WhisperModel.
_whisper_model = None
_whisper_model_lock = threading.Lock()


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


class OllamaProvider(AIProvider):
    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None) -> None:
        self.base_url = base_url or settings.ollama_base_url
        self.model = model or settings.ollama_model

    def generate_structured(self, prompt: str, schema: type[T]) -> T:
        schema_json = schema.model_json_schema()
        raw = self._call_ollama(prompt, format_schema=schema_json)
        try:
            return schema.model_validate_json(raw)
        except ValidationError as first_error:
            logger.warning("AI output failed schema validation, retrying once: %s", first_error)
            correction_prompt = (
                f"{prompt}\n\n"
                f"Your previous response did not match the required schema. "
                f"Validation errors: {first_error}\n"
                "Return ONLY valid JSON matching the schema, with no extra commentary."
            )
            raw_retry = self._call_ollama(correction_prompt, format_schema=schema_json)
            try:
                return schema.model_validate_json(raw_retry)
            except ValidationError as second_error:
                raise AIGenerationError(
                    f"AI output failed schema validation twice: {second_error}"
                ) from second_error

    def generate_text(self, prompt: str) -> str:
        return self._call_ollama(prompt, format_schema=None)

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
        """Dev-only local TTS (Windows SAPI5 via pyttsx3). Swap for a cloud TTS provider
        before real user testing -- voice quality is robotic, not production-grade."""
        import pyttsx3

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            engine = pyttsx3.init()
            engine.save_to_file(text, tmp_path)
            engine.runAndWait()
            return Path(tmp_path).read_bytes()
        except Exception as exc:
            raise AIGenerationError(f"Could not synthesize speech: {exc}") from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _call_ollama(self, prompt: str, format_schema: Optional[dict]) -> str:
        payload: dict = {"model": self.model, "prompt": prompt, "stream": False}
        if format_schema is not None:
            payload["format"] = format_schema

        try:
            response = httpx.post(f"{self.base_url}/api/generate", json=payload, timeout=120)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AIGenerationError(f"Could not reach Ollama at {self.base_url}: {exc}") from exc

        return response.json()["response"]
