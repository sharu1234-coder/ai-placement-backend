"""Code-level backstop against an AI-assigned numeric score landing outside its documented
valid range before it is ever persisted or returned to a student.

A Pydantic schema only validates *type* -- a plain `score: float` field (see AnswerGrade,
GeneratedInterviewEvaluation, GeneratedRecruiterAttackEvaluation in app/ai/schemas/) accepts
50.0 exactly as readily as 0.5, because none of them declare a `Field(ge=..., le=...)` bound.
Every one of those scores is documented, in its own prompt file, as a float from 0.0 to 1.0 --
this module is the deterministic enforcement of that bound, independent of whether the AI
provider actually honored its own instructions. This is the same "never trust the model's own
schema-shaped output as if it were also semantically validated" principle output_guard.py
already applies to leaked prompt scaffolding, just for a different failure mode (an
out-of-range number rather than leaked text)."""

import logging
import math

logger = logging.getLogger(__name__)


def clamp_unit_score(value: float, *, field_name: str, context: str) -> float:
    """Clamp an AI-returned score into the documented [0.0, 1.0] range.

    Never raises -- an out-of-range (or NaN) score is a sign the model didn't follow its own
    prompt instructions, not a reason to fail the whole request and burn the student's AI
    quota on a request that otherwise succeeded. The safest recovery is to clamp to the
    nearest valid bound (or 0.0 for NaN) and keep going, while logging loudly enough that a
    sustained pattern of this would actually be noticed by an operator.

    `field_name`/`context` identify what was clamped and where, without including any actual
    user-derived prompt/answer content -- matches this app's "never log raw user content"
    logging discipline used elsewhere (see anthropic_provider.py/ollama_provider.py)."""
    if math.isnan(value):
        logger.warning("AI returned NaN for %s (%s); clamping to 0.0", field_name, context)
        return 0.0
    if value < 0.0 or value > 1.0:
        clamped = max(0.0, min(1.0, value))
        logger.warning(
            "AI returned out-of-range score %.4f for %s (%s); clamping to %.2f",
            value, field_name, context, clamped,
        )
        return clamped
    return value
