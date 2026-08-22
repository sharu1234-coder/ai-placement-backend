from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AIGenerationError(Exception):
    """Raised when a provider fails to produce valid output (including after a retry)."""


class AIProvider(ABC):
    """Provider-independent AI interface. Services depend only on this, never a vendor SDK."""

    @abstractmethod
    def generate_structured(self, prompt: str, schema: type[T]) -> T:
        """Generate output validated against `schema`. Implementations retry once on
        validation failure with a correction prompt, then raise AIGenerationError."""

    @abstractmethod
    def generate_text(self, prompt: str) -> str:
        ...

    def transcribe(self, audio: bytes) -> str:
        raise NotImplementedError("Voice transcription is not wired yet (Stage 10).")

    def synthesize_speech(self, text: str) -> bytes:
        raise NotImplementedError("Speech synthesis is not wired yet (Stage 10).")


def get_ai_provider() -> AIProvider:
    from app.core.config import settings

    if settings.ai_provider == "ollama":
        from app.ai.ollama_provider import OllamaProvider

        return OllamaProvider()

    raise NotImplementedError(f"AI provider '{settings.ai_provider}' is not implemented yet")
