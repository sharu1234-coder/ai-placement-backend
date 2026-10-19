"""Capability-checked AI provider dependencies for routes.

Why this is a separate module from app/ai/provider.py: that module is imported by services,
and it is deliberately framework-free -- nothing in app/services should acquire a FastAPI
import transitively. HTTP-shaped concerns about the AI layer already live in app/api
(ai_errors.py), so this sits alongside them.

Why `requires_ai` wraps get_ai_provider as a *sub-dependency* rather than calling it: tests
override the provider with `app.dependency_overrides[get_ai_provider]`, and FastAPI applies
an override by substituting the callable it resolves, not by intercepting ordinary Python
calls. A version of this that called `get_ai_provider()` directly inside the function body
would silently bypass every one of those overrides -- the voice tests would start exercising
the real local Whisper/pyttsx3 path instead of their fake. Declaring it with Depends keeps
the override working, and a dependency-injected parameter is not a query parameter, so the
route's OpenAPI schema is unchanged.
"""

from typing import Callable

from fastapi import Depends

from app.ai.provider import AICapability, AIProvider, get_ai_provider


def requires_ai(*capabilities: AICapability) -> Callable[..., AIProvider]:
    """Resolve the configured provider, refusing it unless it declares every capability the
    route needs.

    Used by the voice routes so a provider that cannot do speech is rejected while the request
    is still being set up -- before the upload is read, before the question is looked up, and
    before any model work is attempted. The raised AICapabilityError is handled app-wide in
    app/main.py, which turns it into a 503 rather than a 500.
    """

    def dependency(provider: AIProvider = Depends(get_ai_provider)) -> AIProvider:
        for capability in capabilities:
            provider.require(capability)
        return provider

    return dependency


# Module-level singletons rather than `Depends(requires_ai(...))` written inline at each
# route: ruff's B008 forbids a call in an argument default, and these are the only two
# capability-gated routes in the app, so naming them here also keeps the set visible.
requires_transcribe = requires_ai(AICapability.TRANSCRIBE)
requires_synthesize = requires_ai(AICapability.SYNTHESIZE)
