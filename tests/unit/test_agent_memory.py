"""VA-6: session memory — bounds, isolation, clearing, and what may become a fact.

The rule these exist to protect: a model's guess about a student is not a fact about that
student. Everything else here is about keeping the store small, per-session, and genuinely
deletable, since nothing is persisted and the user's "clear chat" has to mean something.
"""

import datetime

import pytest

from app.agent.contracts import EvidenceKind, Intent
from app.agent.memory import (
    MAX_SESSION_FACTS,
    MAX_SUMMARY_TURNS,
    MemoryItem,
    MemoryOrigin,
    MemoryScope,
    MemoryStore,
    SessionMemory,
)

NOW = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def memory() -> SessionMemory:
    return SessionMemory(session_id="s-1", user_id=1)


class TestModelInferencesAreNeverFacts:
    def test_a_model_inference_is_recorded_as_a_recommendation(self):
        item = SessionMemory(session_id="s", user_id=1).remember(
            "goal", "wants a backend role", origin=MemoryOrigin.MODEL_INFERRED
        )
        assert item.evidence_kind is EvidenceKind.RECOMMENDED

    def test_it_cannot_be_constructed_as_a_known_fact(self):
        with pytest.raises(ValueError) as caught:
            MemoryItem(
                key="goal",
                value="wants a backend role",
                origin=MemoryOrigin.MODEL_INFERRED,
                scope=MemoryScope.SESSION,
                recorded_at=NOW,
                evidence_kind=EvidenceKind.KNOWN,
            )
        assert "cannot be recorded as a known fact" in str(caught.value)

    def test_what_the_user_said_and_what_a_tool_returned_are_known(self, memory):
        assert memory.remember("role", "sde", origin=MemoryOrigin.USER_STATED).evidence_kind is EvidenceKind.KNOWN
        assert (
            memory.remember("score", "61", origin=MemoryOrigin.TOOL_VERIFIED).evidence_kind is EvidenceKind.KNOWN
        )

    def test_the_evidence_kind_cannot_be_chosen_by_the_caller(self, memory):
        # Derived from origin inside `remember`, so there is no argument to misuse.
        import inspect

        assert "evidence_kind" not in inspect.signature(memory.remember).parameters


class TestDurableMemoryIsNotPretended:
    def test_asking_for_durable_scope_is_refused(self, memory):
        # Inventing a durable store to satisfy the word "memory" would create a retention
        # surface for inferences about a student that no approved design covers.
        with pytest.raises(ValueError) as caught:
            memory.remember("pref", "remote only", origin=MemoryOrigin.USER_STATED, scope=MemoryScope.DURABLE)
        assert "no approved persistence design" in str(caught.value)

    def test_the_retention_description_says_nothing_is_written(self, memory):
        memory.remember("role", "sde", origin=MemoryOrigin.USER_STATED)
        described = memory.describe_retention()
        assert "not implemented" in described["durable_preferences"]
        assert "in memory only" in described["ephemeral_facts"]
        assert "never carried forward" in described["turn_evidence"]


class TestBounds:
    def test_facts_are_capped_and_the_oldest_goes_first(self, memory):
        for index in range(MAX_SESSION_FACTS + 4):
            memory.remember(
                f"k{index}",
                f"v{index}",
                origin=MemoryOrigin.USER_STATED,
                now=NOW + datetime.timedelta(seconds=index),
            )
        assert len(memory.facts()) == MAX_SESSION_FACTS
        assert memory.recall("k0") is None
        assert memory.recall(f"k{MAX_SESSION_FACTS + 3}") is not None

    def test_a_value_is_truncated_rather_than_stored_whole(self, memory):
        item = memory.remember("note", "x" * 1_000, origin=MemoryOrigin.USER_STATED)
        assert len(item.value) == MemoryItem.MAX_VALUE

    def test_the_summary_is_a_rolling_window(self, memory):
        for _ in range(MAX_SUMMARY_TURNS + 3):
            memory.record_turn(Intent.PROGRESS, ("progress.get_summary",), answered=True)
        assert len(memory.summary()) == MAX_SUMMARY_TURNS

    def test_the_summary_is_deterministic_not_prose(self, memory):
        memory.record_turn(Intent.WEAKNESSES, ("weaknesses.list",), answered=True)
        line = memory.summary()[0]
        # Built from the intent and tool names, so it cannot drift into an inference that
        # accumulates across turns and becomes a fact nobody verified.
        assert line == "weaknesses via weaknesses.list"

    def test_an_unanswered_turn_is_marked(self, memory):
        memory.record_turn(Intent.JOB_SEARCH, (), answered=False)
        assert "unanswered" in memory.summary()[0]

    def test_confidence_outside_the_range_is_refused(self, memory):
        with pytest.raises(ValueError):
            memory.remember("x", "y", origin=MemoryOrigin.USER_STATED, confidence=1.5)

    def test_absent_confidence_stays_none(self, memory):
        assert memory.remember("x", "y", origin=MemoryOrigin.USER_STATED).confidence is None


class TestDeletion:
    def test_one_fact_can_be_forgotten(self, memory):
        memory.remember("role", "sde", origin=MemoryOrigin.USER_STATED)
        assert memory.forget("role") is True
        assert memory.recall("role") is None
        assert memory.forget("role") is False

    def test_clear_removes_facts_and_the_summary(self, memory):
        memory.remember("role", "sde", origin=MemoryOrigin.USER_STATED)
        memory.record_turn(Intent.PROGRESS, ("progress.get_summary",), answered=True)

        memory.clear()

        # Total rather than selective: a partial clear that left inferences behind would be
        # exactly the surprise this layer should not have.
        assert memory.facts() == () and memory.summary() == ()

    def test_dropping_a_session_removes_it_from_the_store(self):
        store = MemoryStore()
        store.for_session("s-1", user_id=1).remember("role", "sde", origin=MemoryOrigin.USER_STATED)
        store.drop("s-1", user_id=1)
        assert len(store) == 0
        # A fresh session, not the old one resurrected.
        assert store.for_session("s-1", user_id=1).recall("role") is None


class TestSessionIsolation:
    def test_two_sessions_do_not_share_facts(self):
        store = MemoryStore()
        store.for_session("s-1", user_id=1).remember("role", "sde", origin=MemoryOrigin.USER_STATED)
        assert store.for_session("s-2", user_id=1).recall("role") is None

    def test_another_users_session_id_reaches_a_different_conversation(self):
        """A session id is not a credential, and VA-10 made that an isolation *property*.

        It used to be an isolation *check*: the store keyed on the id alone, so the second
        account to name a string was refused with a PermissionError the route rendered as a
        404. Nothing leaked -- but the account that legitimately owned a predictable id (the
        coach route's default is derived from the caller's own user id) could be locked out of
        its own conversation by anyone who guessed it. Keying by account makes the collision
        impossible instead, so the assertion below is the stronger one: the intruder gets a
        fresh, empty conversation, and the owner still has theirs.
        """
        store = MemoryStore()
        store.for_session("s-1", user_id=1).remember(
            "role", "sde", origin=MemoryOrigin.USER_STATED
        )

        intruder = store.for_session("s-1", user_id=2)

        assert intruder.recall("role") is None
        assert intruder.facts() == ()
        assert store.for_session("s-1", user_id=1).recall("role").value == "sde"

    def test_the_ownership_check_still_refuses_a_mismatched_record(self):
        """The check kept behind the new key, proven to still fire.

        Unreachable through `for_session` now, which is the point -- so this reaches past it and
        plants the record a mis-built key would produce. If a future caller derives a key some
        other way, this is what catches it rather than handing over someone else's turn.
        """
        store = MemoryStore()
        store._sessions[store._key("s-1", 1)] = SessionMemory(session_id="s-1", user_id=99)

        with pytest.raises(PermissionError):
            store.for_session("s-1", user_id=1)

    def test_the_same_user_gets_the_same_memory_back(self):
        store = MemoryStore()
        first = store.for_session("s-1", user_id=7)
        first.remember("role", "sde", origin=MemoryOrigin.USER_STATED)
        assert store.for_session("s-1", user_id=7).recall("role").value == "sde"

    def test_the_store_is_bounded(self):
        store = MemoryStore()
        for index in range(MemoryStore.MAX_SESSIONS + 5):
            store.for_session(f"s-{index}", user_id=1)
        assert len(store) == MemoryStore.MAX_SESSIONS
        # Oldest evicted, so an unbounded number of sessions cannot grow the worker.
        assert store.for_session("s-0", user_id=1).recall("role") is None

    def test_reset_clears_everything(self):
        store = MemoryStore()
        store.for_session("s-1", user_id=1)
        store.reset()
        assert len(store) == 0
