import logging
from typing import Optional

import httpx
from pydantic import ValidationError

from app.ai.provider import AIGenerationError, AIProvider, T
from app.core.config import settings

logger = logging.getLogger(__name__)


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
