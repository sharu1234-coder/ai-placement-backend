"""Which turns go to the grounded renderer, and which become ordinary conversation.

This is the fix for the reported defect, as a rule rather than an example. The coach renders
grounded claims only; a turn producing no claims used to land on a fixed sentence -- *"I
checked, and I do not have anything recorded to tell you about that yet."* -- which is why the
Voice Career Coach appeared unable to hold a conversation.

The rule now: **no claims and no failures -> conversation; anything else -> the grounded
renderer.** These tests pin both directions, because getting the first one right by breaking
the second would be a worse outcome than the original defect.
"""

import pytest

from app.agent.coaching import CoachingReport, CoachingTurn
from app.agent.contracts import Intent
from app.voice import responders


class _Provider:
    def __init__(self, reply="Hey! What's on your mind?"):
        self.reply = reply
        self.calls = 0

    def generate_text(self, prompt: str) -> str:
        self.calls += 1
        return self.reply


class _User:
    """The session memory scopes itself by owner, so a bare object() is not enough."""

    id = 1


class _Claim:
    """Minimal stand-in for a grounding Claim: the renderer only reads `.text`."""

    def __init__(self, text):
        self.text = text


def _report(**kwargs) -> CoachingReport:
    """A report, defaulting to the intent that means "this is not a career question".

    `Intent.UNCLEAR` is the default because it is the routing condition under test. A career
    intent has to be stated explicitly, which keeps each test honest about which side of the
    rule it is exercising.
    """
    return CoachingReport(intent=kwargs.pop("intent", Intent.UNCLEAR), **kwargs)


@pytest.fixture
def wired(monkeypatch):
    """Point the responder at a scripted orchestrator and a fake provider."""
    state = {"report": _report(), "provider": _Provider()}

    class _Orchestrator:
        def coach(self, request, context, memory, budget):
            return CoachingTurn(turn=None, report=state["report"])

    monkeypatch.setattr(responders, "_ORCHESTRATOR", _Orchestrator())
    monkeypatch.setattr(responders, "get_ai_provider", lambda: state["provider"])
    # Context and profile lookups need a database; neither is what these tests cover.
    monkeypatch.setattr(responders, "_recent_history", lambda db, **kw: ())
    monkeypatch.setattr(responders, "_profile_id", lambda db, user: 1)
    return state


def _answer(wired, text="Hey, how are you?"):
    respond = responders.build_coach_responder(user=_User(), db=object(), session_key="s-1")
    import asyncio

    return asyncio.run(respond(text))


class TestOrdinaryConversation:
    def test_a_greeting_is_answered_instead_of_refused(self, wired):
        wired["provider"] = _Provider(reply="Hey! I'm doing well. What's on your mind?")

        answer = _answer(wired, "Hey, how are you?")

        assert answer.text == "Hey! I'm doing well. What's on your mind?"
        assert answer.text != responders._NOTHING_TO_SAY

    def test_a_general_knowledge_question_reaches_the_model(self, wired):
        provider = _Provider(reply="A database is an organised store you can query.")
        wired["provider"] = provider

        answer = _answer(wired, "Can you explain what a database is?")

        assert provider.calls == 1
        assert "organised store" in answer.text

    def test_a_chat_reply_is_never_marked_grounded(self, wired):
        # `grounded` means "every sentence traces to a record". A model-authored sentence does
        # not, and mislabelling it would let model text pass as evidence everywhere downstream.
        answer = _answer(wired, "tell me something interesting")
        assert answer.grounded is False

    def test_a_model_failure_falls_back_to_the_truthful_sentence(self, wired):
        class _Broken:
            def generate_text(self, prompt):
                raise RuntimeError("model unreachable")

        wired["provider"] = _Broken()

        answer = _answer(wired, "hey")

        # Not an invented apology: the existing honest fallback.
        assert answer.text == responders._NOTHING_TO_SAY
        assert answer.grounded is False


class TestCareerCoachingIsUnchanged:
    def test_a_turn_with_stored_facts_uses_the_grounded_renderer(self, wired):
        provider = _Provider()
        wired["provider"] = provider
        wired["report"] = _report(
            intent=Intent.SKILL_STATUS,
            stored_facts=(_Claim("Your resume lists Python and SQL."),),
        )

        answer = _answer(wired, "what skills do I have?")

        assert "From your records:" in answer.text
        assert "Your resume lists Python and SQL." in answer.text
        assert answer.grounded is True
        # The whole point: evidence must not be re-written by a model.
        assert provider.calls == 0, "a grounded turn must never reach the chat model"

    def test_a_turn_with_recommendations_uses_the_grounded_renderer(self, wired):
        provider = _Provider()
        wired["provider"] = provider
        wired["report"] = _report(
            intent=Intent.TODAY_PLAN,
            recommendations=(_Claim("Practise two DSA problems a day."),),
        )

        answer = _answer(wired, "what should I do next?")

        assert "What I suggest:" in answer.text
        assert provider.calls == 0

    def test_a_failure_is_reported_rather_than_chatted_over(self, wired):
        # A failed check is real information the student needs. Replacing it with pleasant
        # conversation would hide that live data was unavailable.
        provider = _Provider()
        wired["provider"] = provider
        wired["report"] = _report(
            intent=Intent.JOB_SEARCH,
            failures=("live job market data is not configured",),
        )

        answer = _answer(wired, "what jobs are open right now?")

        assert "Some checks did not complete" in answer.text
        assert "live job market data is not configured" in answer.text
        assert provider.calls == 0, "a reported failure must not be replaced by chat"

    def test_unknowns_are_reported_rather_than_chatted_over(self, wired):
        provider = _Provider()
        wired["provider"] = provider
        wired["report"] = _report(
            intent=Intent.READINESS,
            stored_facts=(_Claim("You target backend roles."),),
            unknowns=("your graduation year",),
        )

        answer = _answer(wired, "am I ready?")

        assert "What I could not establish" in answer.text
        assert provider.calls == 0


class TestAnEmptyAccountStaysHonest:
    def test_a_career_question_with_no_records_is_not_chatted_over(self, wired):
        """The regression the full suite caught, and the reason routing is keyed on intent.

        An earlier version of this fix routed on *claim emptiness*: no claims and no failures
        meant chat. "What are my weaknesses?" on a fresh account is a recognised career
        question (`Intent.WEAKNESSES`) that legitimately produces no claims -- so it went to
        chat, and a friendly non-answer replaced the honest "nothing is recorded".

        `tests/api/test_voice_responders.py::test_an_empty_account_says_so_rather_than_inventing_a_status`
        failed on exactly that, which is the test doing its job. A recognised career question
        keeps the grounded path however empty-handed it is.
        """
        provider = _Provider(reply="Weaknesses are just parts of who you are!")
        wired["provider"] = provider
        # A real classified intent, no claims, no failures: the shape of a fresh account.
        wired["report"] = _report(intent=Intent.WEAKNESSES)

        answer = _answer(wired, "What are my weaknesses?")

        assert provider.calls == 0, "a recognised career question must never reach the chat model"
        assert answer.text == responders._NOTHING_TO_SAY
        assert answer.grounded is False

    def test_out_of_scope_keeps_its_own_refusal(self, wired):
        # OUT_OF_SCOPE is "recognised, and recognised as something this phase will not do".
        # Chatting about it risks implying the product can, so it keeps the grounded path.
        provider = _Provider()
        wired["provider"] = provider
        wired["report"] = _report(intent=Intent.OUT_OF_SCOPE)

        _answer(wired, "apply to this job for me")

        assert provider.calls == 0
