from pydantic import BaseModel


class SpeechCapabilitiesRead(BaseModel):
    """What this deployment can actually do with speech, stated before the client offers it.

    Three separate booleans rather than one `voice_supported`, because the three are configured
    independently and fail independently:

    * `transcribe` and `synthesize` come from the configured AI provider's declared capability
      set (see app/ai/provider.py), which is derived from the methods the adapter actually
      overrides. These gate POST /interviews/{id}/answer/audio and
      GET /interviews/{id}/questions/{qid}/audio respectively.
    * `realtime_voice` comes from STT_PROVIDER in the separate provider-neutral speech layer
      (app/speech/selection.py), which is what the realtime turn runtime recognises audio with.
      A deployment can have one and not the other.

    Collapsing them would make the client guess. A provider that can synthesise but not
    transcribe can read a question aloud and cannot hear the answer, which is not a usable
    voice interview but is also not the same product state as no speech at all.

    Deliberately booleans about operations, never the provider name, the adapter class, the
    configured STT/TTS provider names or any key: a client needs to know what it may offer, not
    what this server is built from.
    """

    transcribe: bool
    synthesize: bool
    realtime_voice: bool
