"""Provider-neutral speech-to-text and text-to-speech (VA-3).

What belongs here: audio in, text out; text in, audio out. Nothing else.

What deliberately does not: model selection for *reasoning*, intent classification, the Tool
Registry, agent orchestration, authorization. A speech provider converts signals. Giving one
the authority to read a user's data or perform a mutation would mean a transcription error
could trigger an action, which is why the boundary is drawn here rather than argued about
later.

The layer is additive. `app/ai/provider.py` keeps its own `transcribe()`/`synthesize_speech()`
and the file-based interview endpoints keep calling them unchanged -- see
`docs/speech-provider-evaluation.md` for why switching them over is a separately approved step
rather than part of VA-3.
"""
