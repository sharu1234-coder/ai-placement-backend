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
        """Default: no voice support. Providers that support it (e.g. OllamaProvider,
        via local Whisper) override this."""
        raise NotImplementedError("This AI provider does not support voice transcription.")

    def synthesize_speech(self, text: str) -> bytes:
        """Default: no voice support. Providers that support it (e.g. OllamaProvider,
        via local TTS) override this. Returns WAV bytes."""
        raise NotImplementedError("This AI provider does not support speech synthesis.")


def get_ai_provider() -> AIProvider:
    from app.core.config import settings

    if settings.ai_provider == "ollama":
        from app.ai.ollama_provider import OllamaProvider

        return OllamaProvider()

    raise NotImplementedError(f"AI provider '{settings.ai_provider}' is not implemented yet")
