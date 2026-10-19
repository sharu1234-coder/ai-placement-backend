"""VA-0.5: the provider capability contract.

The defect this locks down. `AnthropicProvider` implements only text generation, but it
inherited `transcribe`/`synthesize_speech` from `AIProvider`, so `AI_PROVIDER=anthropic`
*selected successfully* and then failed inside the voice call site -- after the request had
been accepted, the audio upload read, and (for synthesis) the question looked up. The failure
surfaced as a generic AI error, so neither the student nor an operator could tell a
misconfigured server from a model that had simply failed.

Two things are being tested, and they are different:

  1. That capabilities cannot lie. They are derived from which methods a subclass actually
     overrides, so there is no hand-maintained list to drift out of step with the code.
  2. That a provider which cannot do the work is refused at *selection*, not at the call
     site -- and that the refusal is typed, so the app can answer 503 rather than 500 while
     still being caught by the `except AIGenerationError` every AI route already has.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest

from app.ai import provider as provider_module
from app.ai.anthropic_provider import AnthropicProvider
from app.ai.ollama_provider import OllamaProvider
from app.ai.provider import (
    AICapability,
    AICapabilityError,
    AIGenerationError,
    AIProvider,
    get_ai_provider,
)
from app.api.ai_deps import requires_ai, requires_synthesize, requires_transcribe

# Every provider get_ai_provider can return, by its AI_PROVIDER setting value. Checked
# against the source below, so adding a third provider without adding it here fails.
REGISTERED = {
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
}


def test_every_provider_get_ai_provider_can_return_is_covered_here():
    """A static guard, because the alternative is a provider nobody ever capability-tested.

    get_ai_provider dispatches on string equality, so the set of supported values is readable
    from the source. If someone adds `settings.ai_provider == "groq"` without extending
    REGISTERED, every other test in this file would still pass while saying nothing about the
    new provider.
    """
    source = inspect.getsource(get_ai_provider)
    in_source = set(re.findall(r'settings\.ai_provider == "([^"]+)"', source))
    assert in_source == set(REGISTERED), (
        f"get_ai_provider supports {sorted(in_source)} but this test covers "
        f"{sorted(REGISTERED)}. Add the new provider to REGISTERED so its capabilities are "
        "actually checked."
    )


@pytest.mark.parametrize("name,cls", sorted(REGISTERED.items()))
def test_declared_capabilities_match_the_implemented_methods(name, cls):
    """No over-claiming and no under-claiming, for every registered provider."""
    assert cls.name == name, f"{cls.__name__}.name should equal its AI_PROVIDER value"

    # Text generation is abstract on AIProvider, so a concrete provider always has it.
    assert AICapability.STRUCTURED in cls.capabilities
    assert AICapability.TEXT in cls.capabilities

    overrides_transcribe = cls.transcribe is not AIProvider.transcribe
    overrides_synthesize = cls.synthesize_speech is not AIProvider.synthesize_speech
    assert (AICapability.TRANSCRIBE in cls.capabilities) is overrides_transcribe
    assert (AICapability.SYNTHESIZE in cls.capabilities) is overrides_synthesize


def test_ollama_declares_voice_and_anthropic_does_not():
    """Pinned explicitly, not just via the rule above: this exact asymmetry is the bug."""
    assert AICapability.TRANSCRIBE in OllamaProvider.capabilities
    assert AICapability.SYNTHESIZE in OllamaProvider.capabilities
    assert AICapability.TRANSCRIBE not in AnthropicProvider.capabilities
    assert AICapability.SYNTHESIZE not in AnthropicProvider.capabilities


def test_capabilities_cannot_be_over_claimed_by_hand():
    """Setting the attribute does not grant the capability -- derivation overwrites it.

    Without this, the "declaration" would be a comment with extra steps: a provider could
    claim speech it has not implemented and the refusal would move back to the call site.
    """

    class OverClaiming(AIProvider):
        capabilities = frozenset(AICapability)  # claims everything

        def generate_structured(self, prompt, schema):  # pragma: no cover - not called
            raise NotImplementedError

        def generate_text(self, prompt):  # pragma: no cover - not called
            raise NotImplementedError

    assert AICapability.TRANSCRIBE not in OverClaiming.capabilities
    assert AICapability.SYNTHESIZE not in OverClaiming.capabilities


def test_require_raises_a_typed_error_that_existing_handlers_already_catch():
    provider = AnthropicProvider(api_key="not-a-real-key")
    with pytest.raises(AICapabilityError) as caught:
        provider.require(AICapability.TRANSCRIBE)

    # The subclass relationship is load-bearing: every AI route does `except
    # AIGenerationError`, so this must not escape one as an unhandled 500.
    assert isinstance(caught.value, AIGenerationError)
    assert caught.value.provider == "anthropic"
    assert caught.value.capability is AICapability.TRANSCRIBE
    # Actionable: it must say what to change, not just that something is wrong.
    assert "AI_PROVIDER" in str(caught.value)


def test_require_is_silent_for_a_capability_the_provider_has():
    AnthropicProvider(api_key="not-a-real-key").require(AICapability.TEXT)


@pytest.mark.parametrize("method,args", [("transcribe", (b"audio",)), ("synthesize_speech", ("hi",))])
def test_calling_an_unsupported_method_directly_is_also_typed(method, args):
    """Belt and braces: the dependency is the gate, but a service calling the method directly
    must not get a bare NotImplementedError, which no route maps to anything sensible."""
    provider = AnthropicProvider(api_key="not-a-real-key")
    with pytest.raises(AICapabilityError):
        getattr(provider, method)(*args)


def test_get_ai_provider_stays_parameterless():
    """FastAPI exposes a dependency's parameters as query parameters.

    get_ai_provider is `Depends()`-ed by ~20 routes, so adding an argument to it -- the
    obvious way to pass a capability -- would silently add a query parameter to every one of
    them and change the public API. That is why requires_ai exists instead.
    """
    assert list(inspect.signature(get_ai_provider).parameters) == []


def test_requires_ai_takes_the_provider_as_a_sub_dependency():
    """So that `app.dependency_overrides[get_ai_provider]` still works.

    FastAPI applies an override by substituting the callable it resolves. A requires_ai that
    called get_ai_provider() in its body would bypass every override in the suite, and the
    voice tests would quietly start loading the real Whisper model.
    """
    params = inspect.signature(requires_ai(AICapability.TEXT)).parameters
    assert list(params) == ["provider"]
    assert params["provider"].default.dependency is get_ai_provider


def test_the_capability_gated_dependencies_are_the_voice_ones():
    for dependency, capability in (
        (requires_transcribe, AICapability.TRANSCRIBE),
        (requires_synthesize, AICapability.SYNTHESIZE),
    ):
        with pytest.raises(AICapabilityError) as caught:
            dependency(provider=AnthropicProvider(api_key="not-a-real-key"))
        assert caught.value.capability is capability

        # And it passes a provider that can do the work through unchanged.
        passed = dependency(provider=OllamaProvider())
        assert passed.supports(capability)


def test_the_provider_module_stays_framework_free():
    """app/services imports app.ai.provider; it must not acquire FastAPI transitively.

    Checked over the parsed import statements rather than the file text, because the module's
    comments legitimately discuss FastAPI -- the first version of this test searched the raw
    source and failed on its own explanatory prose.
    """
    tree = ast.parse(Path(inspect.getfile(provider_module)).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "fastapi" not in imported, (
        f"app/ai/provider.py imports {sorted(imported)}, which includes FastAPI. The "
        "capability dependency belongs in app/api/ai_deps.py so services stay framework-free."
    )
