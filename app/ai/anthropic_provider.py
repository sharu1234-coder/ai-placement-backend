import logging
from typing import Optional

import anthropic
from pydantic import ValidationError

from app.ai.provider import AIGenerationError, AIProvider, T
from app.core.config import settings

logger = logging.getLogger(__name__)

# All of this app's structured schemas (questions, grades, JD parses, interview
# evaluations, roadmap task lists) comfortably fit well under this; sized generously per
# "don't lowball max_tokens" rather than tuned to the smallest schema.
_MAX_TOKENS = 16000


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
    generate_text, i.e. text generation), so both fall through to AIProvider's
    NotImplementedError default until that work lands.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        key = api_key or settings.anthropic_api_key
        if not key:
            raise AIGenerationError(
                "ANTHROPIC_API_KEY is not set -- required when AI_PROVIDER=anthropic."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self.model = model or settings.anthropic_model

    def generate_structured(self, prompt: str, schema: type[T]) -> T:
        try:
            return self._call_parse(prompt, schema)
        except ValidationError as first_error:
            logger.warning("AI output failed schema validation, retrying once: %s", first_error)
            correction_prompt = (
                f"{prompt}\n\n"
                f"Your previous response did not match the required schema. "
                f"Validation errors: {first_error}\n"
                "Return ONLY corrected structured data."
            )
            try:
                return self._call_parse(correction_prompt, schema)
            except ValidationError as second_error:
                raise AIGenerationError(
                    f"AI output failed schema validation twice: {second_error}"
                ) from second_error

    def _call_parse(self, prompt: str, schema: type[T]) -> T:
        # output_format enforces the JSON schema server-side (same guarantee OllamaProvider
        # gets from Ollama's `format=` parameter) -- a ValidationError here means the model
        # produced text that still didn't conform, not a network/auth problem.
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
            )
        except anthropic.APIStatusError as exc:
            raise AIGenerationError(f"Anthropic API call failed ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIGenerationError(f"Could not reach the Anthropic API: {exc}") from exc

        if response.parsed_output is None:
            raise AIGenerationError("Anthropic response did not include structured output")
        return response.parsed_output

    def generate_text(self, prompt: str) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as exc:
            raise AIGenerationError(f"Anthropic API call failed ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIGenerationError(f"Could not reach the Anthropic API: {exc}") from exc

        return "".join(block.text for block in response.content if block.type == "text")
