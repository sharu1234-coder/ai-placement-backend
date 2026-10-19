"""Ordinary conversation for the Voice Career Coach.

## Why this module exists

The coach responder in `responders.py` renders *grounded claims only*: four sections of
evidence drawn from the student's own records, plus whatever the grounding layer could not
establish. That is a deliberate rule, and `_speak_coaching` states it -- "a summary of evidence
is a new claim about it".

The consequence was that the coach could not hold a conversation. "Hey, how are you?" produces
no claim about anybody's records, so the orchestrator returned nothing and the student heard a
fixed sentence: *"I checked, and I do not have anything recorded to tell you about that yet."*
Reported as "it does not respond to normal conversation", and accurately so -- the architecture
had no path for a turn that was not a question about stored evidence.

## How this preserves the grounding rule rather than breaking it

Chat answers are marked `grounded=False`. That matters: `grounded` is the flag the rest of the
system uses to mean "every sentence here traces to recorded evidence", and a model-authored
sentence does not. So the two kinds of reply stay distinguishable all the way out to the
client, and nothing here can launder model text into a grounded claim.

The division of labour is therefore: if the orchestrator found evidence, the student hears the
evidence; if it found none, the student gets a conversation instead of a dead end. Career
questions keep the grounded path they always had.

## What the persona may not do

The prompt forbids claiming to be human, inventing experiences or feelings, and asserting
facts the model cannot support -- particularly anything time-sensitive. Those are instructions
to the model, which means they are *mitigations, not guarantees*: a local model can still be
wrong. That is why chat replies carry `grounded=False` rather than being presented with the
same confidence as a record lookup.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from app.ai.output_guard import looks_like_prompt_leak

logger = logging.getLogger(__name__)

#: How many previous turns of context to send.
#:
#: Eight, matching `MAX_SUMMARY_TURNS` in the agent's session memory, so the two notions of
#: "recent conversation" do not disagree. It is a bound rather than a preference: the whole
#: transcript would grow without limit across a long session, and a local model's context
#: window is the first thing that breaks when it does.
MAX_CONTEXT_TURNS = 8

#: The longest reply worth speaking without being asked for detail.
#:
#: Spoken answers are listened to, not skimmed, and a paragraph that reads briskly on screen is
#: a long time to sit through. The model is asked for brevity; this is not enforced by
#: truncation, because cutting a sentence in half mid-word is worse than a reply that ran long.
_BREVITY_SENTENCES = 3

_PERSONA = (
    "You are the voice of TALVIORA AI, a career companion for students in India. "
    "You are speaking out loud, so write the way a person talks.\n"
    "\n"
    "Who you are:\n"
    "- Warm, relaxed and genuinely interested. Casually professional, never stiff.\n"
    "- Clear everyday English. Use contractions. Short sentences.\n"
    "- Calm and supportive when someone is worried; direct and practical when they want a "
    "plan.\n"
    "\n"
    "How you reply:\n"
    f"- Keep it to about {_BREVITY_SENTENCES} sentences unless the student asks for more "
    "detail, then give as much as they need.\n"
    "- Answer what was actually asked. Do not steer every exchange back to careers; if "
    "someone wants to chat, chat.\n"
    "- Vary your openings. Never begin consecutive replies with the same word or phrase.\n"
    "- Ask at most one follow-up question, and only when it genuinely helps. Do not "
    "interrogate.\n"
    "- No filler enthusiasm, no exclamation marks in every line, no customer-service "
    "phrasing like 'I would be happy to assist you'.\n"
    "- Plain spoken prose only: no markdown, bullet points, numbered lists or emoji.\n"
    "\n"
    "What you must not do:\n"
    "- Never claim to be a human, and never invent personal experiences, feelings, "
    "qualifications or memories.\n"
    "- Never invent facts about the student. If you were not told something, you do not "
    "know it.\n"
    "- For anything that changes over time -- current job openings, salaries, deadlines, "
    "cut-offs, news -- say plainly that you cannot check it live, and offer what you can "
    "reason about instead.\n"
    "- If you are not sure, say so. A short honest answer beats a confident wrong one."
)


def build_prompt(text: str, history: Sequence[tuple[str, str]]) -> str:
    """Assemble the chat prompt from the persona, recent turns and the new utterance.

    `history` is (role, text) oldest-first, already bounded and already owner-scoped by the
    caller -- this function does no authorization of its own and must never be handed turns
    that were not resolved through `conversation_history.get_conversation`.
    """
    lines = [_PERSONA, ""]
    recent = list(history)[-MAX_CONTEXT_TURNS:]
    if recent:
        lines.append("The conversation so far:")
        for role, said in recent:
            speaker = "Student" if role == "user" else "You"
            lines.append(f"{speaker}: {said}")
        lines.append("")
    lines.append(f"Student: {text}")
    lines.append("You:")
    return "\n".join(lines)


def _clean(reply: str) -> str:
    """Strip the scaffolding a local model tends to echo back.

    Small instruct models frequently prefix the speaker label they were shown, and some wrap
    the reply in quotes. Both are read aloud verbatim by the synthesiser, so "You: Hey there"
    becomes the assistant literally saying the word "you" first.
    """
    cleaned = reply.strip()
    for prefix in ("You:", "Assistant:", "TALVIORA:", "AI:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    return cleaned


def chat_reply(
    text: str,
    *,
    history: Sequence[tuple[str, str]],
    provider,
) -> Optional[str]:
    """One conversational reply, or None if the model could not produce a usable one.

    Returns None rather than raising, and rather than substituting an apology. The caller
    already has a truthful fallback for "I have nothing to say", and inventing a cheerful
    recovery line here would make a failed model call indistinguishable from a real answer.

    The utterance is never logged. `logger` records that a turn failed and the exception type,
    which is what diagnosing a provider outage needs; the student's words are not that.
    """
    prompt = build_prompt(text, history)
    try:
        reply = provider.generate_text(prompt)
    except Exception as exc:
        logger.warning(
            "voice chat turn could not be generated (%s)", type(exc).__name__
        )
        return None

    cleaned = _clean(reply or "")
    if not cleaned:
        return None

    if looks_like_prompt_leak(cleaned):
        # The model echoed its own instructions instead of answering. Caught here because
        # `test_every_production_ai_call_site_can_reach_the_output_guard` requires every
        # generation call site to be able to reach the guard -- and it caught this one
        # missing it.
        #
        # It matters more here than almost anywhere else in the product: this prompt *is* a
        # list of behavioural rules, so a leak would have the assistant read "never claim to
        # be a human" aloud to a student, and the gateway would persist it as a turn.
        #
        # Dropped rather than repaired. There is no way to salvage a reply that is really the
        # prompt, and the caller's truthful fallback is the correct outcome.
        logger.warning("voice chat reply discarded: prompt scaffolding detected")
        return None

    return cleaned
