"""What this deployment can actually do, asked before the client offers it.

Exists because the client had no way to find out. The capability model in app/ai/provider.py
answers it correctly on the server and the two speech-gated routes refuse correctly with a 503
-- but a refusal arriving after the student has chosen to speak is a dead end, and the Android
client rendered it as a generic retryable server error. This route moves the same answer to
*before* the choice.

Why a route of its own rather than a field on GET /users/me: this is a property of the server,
not of the student, and the interview flow does not read the profile. Putting it there would
mean fetching a response full of personal data (name, email, mobile) into screens that have no
use for any of it, just to learn whether a microphone button should exist.

Authenticated on purpose. The answer is not a secret -- it is published to every signed-in
client -- but an unauthenticated probe of a deployment's AI configuration is free
reconnaissance, and nothing here needs to be reachable before login.
"""

from fastapi import APIRouter, Depends

from app.ai.provider import AICapability, AIProvider, get_ai_provider
from app.api.deps import get_current_user
from app.core.config import settings
from app.models.user import User
from app.schemas.capabilities import SpeechCapabilitiesRead
from app.speech.selection import STT_PROVIDERS

router = APIRouter(prefix="/capabilities", tags=["capabilities"])

def speech_capabilities(provider: AIProvider) -> SpeechCapabilitiesRead:
    """What `provider` and the configured speech layer can do, as the public contract.

    Takes the provider as an argument rather than calling `get_ai_provider()` itself. That is
    not a style choice: app/api/ai_deps.py documents that resolving the provider with a plain
    call inside a function body silently bypasses every
    `app.dependency_overrides[get_ai_provider]`, so a route written that way cannot be given a
    test double and the voice tests would exercise the real local Whisper path. The route below
    resolves it with `Depends` and hands it here, which keeps overrides working and keeps this
    function a pure, framework-free mapping that is unit-testable on its own.

    Note where the fail-safe is NOT: there is deliberately no try/except here turning an
    unresolvable provider into all-false. A client must already treat *any* failure to read
    this contract as "no voice" -- including the 404 it gets from a backend that predates this
    route, which no amount of server-side handling could cover -- so the safe default belongs
    on the client, and duplicating it here would only hide a misconfiguration from the operator
    who needs to see it.
    """
    stt = (settings.stt_provider or "").strip().lower()
    # A name this build does not recognise is not a configured recogniser. Mirrors
    # get_speech_to_text, which refuses an unknown name rather than falling back to one.
    realtime = stt in STT_PROVIDERS and stt != "none"

    return SpeechCapabilitiesRead(
        transcribe=provider.supports(AICapability.TRANSCRIBE),
        synthesize=provider.supports(AICapability.SYNTHESIZE),
        # TTS deliberately does not appear here. Its absence degrades rather than blocks: the
        # realtime path shows the question as text when synthesis is unavailable, so a missing
        # TTS provider does not make the conversation impossible the way a missing recogniser
        # does. Treating them as one flag would hide a working feature.
        realtime_voice=realtime,
    )


@router.get("/speech", response_model=SpeechCapabilitiesRead)
def get_speech_capabilities(
    current_user: User = Depends(get_current_user),
    provider: AIProvider = Depends(get_ai_provider),
) -> SpeechCapabilitiesRead:
    return speech_capabilities(provider)
