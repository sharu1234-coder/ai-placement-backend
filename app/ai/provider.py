from abc import ABC, abstractmethod
from enum import Enum
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AICapability(str, Enum):
    """What a provider can actually do.

    This exists because "the interface has a method" and "this provider implements it" were
    the same question in the old design, and they are not. AnthropicProvider inherits
    transcribe()/synthesize_speech() from AIProvider without implementing either, so
    AI_PROVIDER=anthropic selected successfully and then failed at the voice call site --
    after the request had been accepted, the upload read and (for synthesis) the question
    already looked up. The capability set makes that answerable before the work starts.
    """

    STRUCTURED = "structured"
    TEXT = "text"
    TRANSCRIBE = "transcribe"
    SYNTHESIZE = "synthesize"


class AIGenerationError(Exception):
    """Raised when a provider fails to produce valid output (including after a retry)."""


class AICapabilityError(AIGenerationError):
    """Raised when the configured provider cannot perform the requested kind of work.

    Deliberately a subclass of AIGenerationError. Every route that already handles AI failure
    does `except AIGenerationError` (see app/api/ai_errors.py), so this is caught by the
    existing handlers and surfaces as the existing generic 422 rather than a 500 -- the
    interview-audio API contract does not change because of this class. The distinct type is
    what lets `requires_ai` reject the request earlier, and what lets an operator tell
    "misconfigured provider" apart from "the model failed" in the logs.
    """

    def __init__(self, provider: str, capability: AICapability, *, reason: str = "") -> None:
        self.provider = provider
        self.capability = capability
        self.reason = reason
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"AI provider '{provider}' does not support '{capability.value}'{detail}. "
            f"Set AI_PROVIDER to a provider that declares the '{capability.value}' capability, "
            "or configure that capability's backing service."
        )


class AIProvider(ABC):
    """Provider-independent AI interface. Services depend only on this, never a vendor SDK."""

    #: Derived automatically in __init_subclass__ from which methods the subclass actually
    #: implements. Deliberately NOT hand-maintained: a hand-written set is a second source of
    #: truth that can disagree with the code, and the disagreement would reintroduce exactly
    #: the bug this change exists to remove -- a provider that claims speech and then raises
    #: at the call site. It also means a test double cannot be wrong about itself.
    capabilities: frozenset[AICapability] = frozenset()

    #: Provider name used in errors and logs. Matches the AI_PROVIDER setting value.
    name: str = "unknown"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # STRUCTURED and TEXT are abstract on this class, so any concrete subclass has them.
        derived = {AICapability.STRUCTURED, AICapability.TEXT}
        # The voice methods have working defaults that raise, so "implements it" means
        # "overrides it" -- compared against this class's own function objects.
        if cls.transcribe is not AIProvider.transcribe:
            derived.add(AICapability.TRANSCRIBE)
        if cls.synthesize_speech is not AIProvider.synthesize_speech:
            derived.add(AICapability.SYNTHESIZE)
        cls.capabilities = frozenset(derived)
        # Fall back to the class name rather than "unknown". A refusal is logged with the
        # provider name, and "provider=unknown" tells an operator nothing about which class
        # was actually selected -- observed in exactly that form while developing this.
        if "name" not in cls.__dict__ and cls.name == AIProvider.name:
            cls.name = cls.__name__

    @abstractmethod
    def generate_structured(self, prompt: str, schema: type[T]) -> T:
        """Generate output validated against `schema`. Implementations retry once on
        validation failure with a correction prompt, then raise AIGenerationError."""

    @abstractmethod
    def generate_text(self, prompt: str) -> str:
        ...

    def supports(self, capability: AICapability) -> bool:
        return capability in self.capabilities

    def require(self, capability: AICapability) -> None:
        """Raise if this provider cannot do `capability`. Called at selection time."""
        if not self.supports(capability):
            raise AICapabilityError(self.name, capability)

    def transcribe(self, audio: bytes) -> str:
        """Default: no voice support. Providers declaring AICapability.TRANSCRIBE override this."""
        raise AICapabilityError(self.name, AICapability.TRANSCRIBE)

    def synthesize_speech(self, text: str) -> bytes:
        """Default: no voice support. Providers declaring AICapability.SYNTHESIZE override
        this. Returns WAV bytes."""
        raise AICapabilityError(self.name, AICapability.SYNTHESIZE)


def get_ai_provider() -> AIProvider:
    """The FastAPI dependency every AI route already uses.

    The signature must stay parameterless: FastAPI inspects a dependency's signature and
    would expose any added argument as a query parameter, which would change the public API
    of ~20 routes. Capability checking is therefore done by `requires_ai` below, which closes
    over the capability instead of taking it as an argument.
    """
    from app.core.config import settings

    if settings.ai_provider == "ollama":
        from app.ai.ollama_provider import OllamaProvider

        return OllamaProvider()

    if settings.ai_provider == "anthropic":
        from app.ai.anthropic_provider import AnthropicProvider

        return AnthropicProvider()

    raise NotImplementedError(f"AI provider '{settings.ai_provider}' is not implemented yet")


# The capability-checking FastAPI dependency lives in app/api/ai_deps.py, not here: this
# module is deliberately framework-free (services import it, and they must not acquire a
# FastAPI dependency on the way). See that module for why it must wrap get_ai_provider as a
# real sub-dependency rather than calling it.
