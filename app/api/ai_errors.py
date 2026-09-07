"""One place to turn an AIGenerationError into an HTTP response.

The problem this fixes. Six routes (assessments x2, interviews x2, roadmap x2) used to do:

    except AIGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

and `str(exc)` is not a message written for a student. app/ai/anthropic_provider.py builds
several of these by interpolating the provider's own error text --
`f"Anthropic API call failed ({exc.status_code}): {exc.message}"` and
`f"Could not reach the Anthropic API: {exc}"` -- so a misconfigured key would have shown every
user of the app a 422 body reading "Anthropic API call failed (401): invalid x-api-key", and a
network fault could surface internal host or proxy details the same way. It also names the AI
vendor to anyone who can trigger a failure.

The app already had the right pattern for exactly this, one module over: subscription_service
logs Razorpay's response body server-side (truncated) and hands the caller a fixed sentence.
This does the same for the AI provider, so the diagnostic detail stays in the logs where an
operator can find it by request id, and the client gets something a person can act on.
"""

import logging

from fastapi import HTTPException, status

from app.ai.provider import AIGenerationError

# Deliberately fixed and provider-agnostic: it must not vary with the failure, or the
# variation itself becomes the disclosure this module exists to prevent.
_CLIENT_MESSAGE = "The AI service is temporarily unavailable. Please try again in a moment."


def ai_unavailable(exc: AIGenerationError, logger: logging.Logger, context: str) -> HTTPException:
    """Log the real reason, return a generic 422 for the client to raise.

    Returns rather than raises so the call site keeps reading as `raise ai_unavailable(...)`,
    which keeps the control flow visible in the route instead of hiding it in a helper.
    """
    logger.warning("AI generation failed (%s): %s", context, exc)
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_CLIENT_MESSAGE
    )
