"""Realtime voice transport (VA-2).

Transport only: session establishment, framing, limits and lifecycle. No speech recognition,
no synthesis, no model selection, no tool execution. Those arrive in later phases and must not
leak into this package -- a gateway that quietly grew an opinion about models would be exactly
the vendor coupling the architecture exists to prevent.
"""
