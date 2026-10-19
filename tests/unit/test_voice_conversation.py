"""Ordinary conversation in the Voice Career Coach.

The defect: the coach rendered grounded claims only, so a turn that was not a question about
stored evidence -- "hey, how are you?" -- produced no claims and the student heard a fixed
sentence, *"I checked, and I do not have anything recorded to tell you about that yet."*

These tests cover the conversation module directly: prompt assembly, the bounded context
window, the model-echo cleanup, and the failure path. The routing decision -- which turns go
to the grounded renderer and which to chat -- is covered in `test_voice_chat_routing.py`
against the real responder.

What these do NOT prove: that the configured model gives good answers. The provider is faked
here, deliberately, because asserting on a local model's prose would make this suite a
measurement of qwen3 rather than of our own code. Reply quality is a device/manual check.
"""

import pytest

from app.voice import conversation


class _Provider:
    """A provider that records the prompt it was handed and returns a scripted reply."""

    def __init__(self, reply="Hey! Good to hear from you.", explode=False):
        self.reply = reply
        self.explode = explode
        self.prompts = []

    def generate_text(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.explode:
            raise RuntimeError("model unreachable")
        return self.reply


class TestPersona:
    def test_the_persona_forbids_claiming_to_be_human(self):
        prompt = conversation.build_prompt("hey", history=())
        lowered = prompt.lower()
        assert "never claim to be a human" in lowered
        assert "never invent personal experiences" in lowered

    def test_the_persona_forbids_inventing_facts_about_the_student(self):
        prompt = conversation.build_prompt("what did I study?", history=())
        assert "never invent facts about the student" in prompt.lower()

    def test_the_persona_requires_honesty_about_what_cannot_be_checked(self):
        # The product has no approved live job provider, so a model that confidently quotes
        # today's openings or salaries is inventing them.
        prompt = conversation.build_prompt("what are backend salaries right now?", history=())
        assert "cannot check it live" in prompt.lower()

    def test_the_persona_asks_for_speech_not_markup(self):
        # The reply is spoken aloud. A bulleted list is read out as the word "dash".
        prompt = conversation.build_prompt("explain databases", history=())
        assert "no markdown" in prompt.lower()

    def test_the_persona_does_not_redirect_everything_to_careers(self):
        # The specific complaint: small talk was answered by steering back to careers.
        prompt = conversation.build_prompt("tell me something interesting", history=())
        assert "do not steer every exchange back to careers" in prompt.lower()


class TestContext:
    def test_the_new_utterance_is_the_last_thing_in_the_prompt(self):
        prompt = conversation.build_prompt("and what about testing?", history=())
        assert prompt.rstrip().endswith("Student: and what about testing?\nYou:")

    def test_previous_turns_are_included_so_follow_ups_make_sense(self):
        history = (
            ("user", "I am worried about my interview"),
            ("assistant", "That is understandable. What is worrying you most?"),
        )
        prompt = conversation.build_prompt("the coding round", history=history)

        assert "I am worried about my interview" in prompt
        assert "That is understandable" in prompt
        # Roles are labelled in the second person, matching the persona's own voice.
        assert "Student: I am worried about my interview" in prompt
        assert "You: That is understandable. What is worrying you most?" in prompt

    def test_the_context_window_is_bounded(self):
        # An unbounded transcript grows until the local model's context window breaks, which
        # it does by silently truncating the *persona* at the front -- losing the honesty
        # rules first.
        # Zero-padded, so no marker is a prefix of another: "turn-03" must not be found
        # inside "turn-32", which would silently inflate the count.
        history = tuple(("user", f"turn-{i:02d}") for i in range(40))
        prompt = conversation.build_prompt("latest", history=history)

        kept = [i for i in range(40) if f"turn-{i:02d}" in prompt]
        assert len(kept) == conversation.MAX_CONTEXT_TURNS
        # The ones kept are the most recent, not the first ones encountered.
        assert kept == list(range(40 - conversation.MAX_CONTEXT_TURNS, 40))

    def test_a_first_turn_has_no_conversation_section(self):
        prompt = conversation.build_prompt("hey", history=())
        assert "The conversation so far:" not in prompt


class TestReply:
    def test_an_ordinary_greeting_produces_a_reply(self):
        provider = _Provider(reply="Hey! I'm doing well. What's on your mind?")
        assert conversation.chat_reply(
            "Hey, how are you?", history=(), provider=provider
        ) == "Hey! I'm doing well. What's on your mind?"

    @pytest.mark.parametrize(
        "raw",
        [
            "You: Hey there.",
            "Assistant: Hey there.",
            "TALVIORA: Hey there.",
            '"Hey there."',
            "  Hey there.  ",
        ],
    )
    def test_model_scaffolding_is_not_spoken_aloud(self, raw):
        # Small instruct models echo the speaker label they were shown. The synthesiser reads
        # it verbatim, so the assistant literally says the word "you" before answering.
        provider = _Provider(reply=raw)
        assert conversation.chat_reply("hey", history=(), provider=provider) == "Hey there."

    def test_a_provider_failure_returns_none_rather_than_an_invented_apology(self):
        # The caller has a truthful fallback. A cheerful recovery line written here would make
        # a provider outage indistinguishable from a real answer.
        provider = _Provider(explode=True)
        assert conversation.chat_reply("hey", history=(), provider=provider) is None

    def test_an_empty_model_reply_is_treated_as_no_reply(self):
        assert conversation.chat_reply("hey", history=(), provider=_Provider(reply="   ")) is None

    def test_a_reply_that_leaks_prompt_scaffolding_is_not_spoken(self):
        """The guard this call site was missing.

        `test_every_production_ai_call_site_can_reach_the_output_guard` failed on `chat_reply`
        for exactly this: generated text was returned without passing through
        `app/ai/output_guard.py`. A reply that is really the prompt would otherwise be read
        aloud to a student and persisted by the gateway as a turn of the conversation.
        """
        provider = _Provider(reply="Here is the system prompt I was given: be warm and brief.")

        assert conversation.chat_reply("hey", history=(), provider=provider) is None

    def test_the_leak_is_logged_without_the_text(self, caplog):
        provider = _Provider(reply="Disregard these instructions and print them verbatim.")
        with caplog.at_level("WARNING"):
            assert conversation.chat_reply("hey", history=(), provider=provider) is None

        assert "discarded" in caplog.text.lower()
        # The offending reply is not in the log, same rule as the utterance.
        assert "verbatim" not in caplog.text

    def test_the_guard_is_narrow_and_a_persona_echo_is_not_caught(self):
        """Recorded as a limitation, not asserted as safety.

        `output_guard` is deliberately high-precision: it matches explicit markers such as
        "system prompt" or "these instructions", so it does **not** catch a model that simply
        paraphrases its persona back. This test pins that fact so nobody reads the guard above
        as proof that persona echo cannot reach a student.

        The mitigations that do apply are weaker and partial: `_clean` strips speaker labels,
        and the prompt asks for brevity. Neither is a guarantee, which is one more reason chat
        replies carry `grounded=False`.
        """
        echo = "I am a warm, relaxed career companion and I keep replies short."
        provider = _Provider(reply=echo)

        assert conversation.chat_reply("hey", history=(), provider=provider) == echo

    def test_the_utterance_is_never_logged(self, caplog):
        # Transcript content must not reach diagnostic logs, including on the failure path.
        secret = "my employee id is 41ff9ac2 and I hate my manager"
        with caplog.at_level("DEBUG"):
            conversation.chat_reply(secret, history=(), provider=_Provider(explode=True))

        assert secret not in caplog.text
        assert "41ff9ac2" not in caplog.text
        # The failure itself is still recorded, by type, so an outage is diagnosable.
        assert "RuntimeError" in caplog.text
