"""Conversation state and memory for one agent session.

What persists: **nothing.** This is an in-process, bounded, per-session store that dies with
the worker. That is a deliberate design decision rather than an unfinished one, for the reason
the brief itself names: inventing a durable store and a migration to satisfy the word "memory"
would create a retention surface for inferences about a student that no approved design covers.
Durable, user-approved preferences are deferred until such a design exists.

What that costs, stated plainly: a session cannot be resumed on another worker, and a restart
loses the conversation. That is the same limitation the VA-2 voice registry already has and is
recorded alongside it rather than presented as solved.

Four kinds of state, kept separate because they have different lifetimes and different
trustworthiness:

1. **Ephemeral session facts** -- what the user said this session (a role they mentioned, a
   city). Scoped to the session, cleared on `clear()`.
2. **Current-turn evidence** -- tool results for the turn being answered. Discarded at the end
   of the turn, because evidence has a retrieval time and reusing it next turn would be
   presenting stale data as fresh.
3. **Rolling summary** -- a bounded list of what happened, built *deterministically* from
   intents and tool names. Not model-generated: a summary written by a model is an inference,
   and an inference that accumulates across turns becomes a fact nobody verified.
4. **Durable preferences** -- not implemented. See above.

Every item records origin, timestamp, scope, confidence and how it is deleted, because those
five things are what make it possible to answer "why does the agent think this about me?".
"""

from __future__ import annotations

import datetime
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.agent.contracts import EvidenceKind, Intent

#: Turns kept in the rolling summary. Small on purpose: the summary exists so the agent can
#: resolve "and in Pune?" against the previous question, not to reconstruct the conversation.
MAX_SUMMARY_TURNS = 8

#: Ephemeral facts held per session.
MAX_SESSION_FACTS = 16


class MemoryScope(str, Enum):
    """How far an item reaches."""

    TURN = "turn"
    SESSION = "session"
    #: Reserved, unused in VA-6. Present so that adding durable memory later has to choose
    #: this explicitly rather than inherit SESSION by default.
    DURABLE = "durable"


class MemoryOrigin(str, Enum):
    """Where an item came from. The field that stops an inference becoming a fact."""

    #: The user said it in this session.
    USER_STATED = "user_stated"
    #: An authenticated tool returned it.
    TOOL_VERIFIED = "tool_verified"
    #: A model produced it. Never storable as a verified fact -- see `remember`.
    MODEL_INFERRED = "model_inferred"


@dataclass(frozen=True)
class MemoryItem:
    """One remembered thing, with everything needed to judge and delete it."""

    key: str
    value: str
    origin: MemoryOrigin
    scope: MemoryScope
    recorded_at: datetime.datetime
    #: The evidence kind this item may be used as. Derived from origin, never set freely.
    evidence_kind: EvidenceKind
    #: 0.0--1.0 where a source supplies one, else None. Never invented.
    confidence: Optional[float] = None

    MAX_VALUE = 200

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", self.value.strip()[: self.MAX_VALUE])
        if self.origin is MemoryOrigin.MODEL_INFERRED and self.evidence_kind is EvidenceKind.KNOWN:
            # The rule the whole module exists to enforce: a model's guess about a student is
            # not a fact about that student, however confident it sounds.
            raise ValueError("a model inference cannot be recorded as a known fact")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in 0.0--1.0")

    def describe(self) -> str:
        return f"{self.key}={self.value} ({self.origin.value}, {self.scope.value})"


@dataclass(frozen=True)
class TurnRecord:
    """One line of the rolling summary. Deterministic, not prose."""

    intent: Intent
    tools_used: tuple[str, ...]
    at: datetime.datetime
    answered: bool

    def describe(self) -> str:
        tools = ", ".join(self.tools_used) if self.tools_used else "no tools"
        return f"{self.intent.value} via {tools}{'' if self.answered else ' (unanswered)'}"


@dataclass
class SessionMemory:
    """One conversation's state. In-process, bounded, not persisted.

    Lock-guarded because the sync routes run in a threadpool and two frames of one session can
    be handled by different threads.
    """

    session_id: str
    user_id: int
    _facts: dict[str, MemoryItem] = field(default_factory=dict)
    _turns: deque = field(default_factory=lambda: deque(maxlen=MAX_SUMMARY_TURNS))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ facts

    def remember(
        self,
        key: str,
        value: str,
        *,
        origin: MemoryOrigin,
        confidence: Optional[float] = None,
        scope: MemoryScope = MemoryScope.SESSION,
        now: Optional[datetime.datetime] = None,
    ) -> MemoryItem:
        """Record an ephemeral fact.

        `evidence_kind` is derived from `origin` rather than accepted as an argument, so a
        caller cannot label a model's guess as verified. `DURABLE` scope is refused outright:
        there is no approved persistence design, and accepting the scope would imply there is.
        """
        if scope is MemoryScope.DURABLE:
            raise ValueError("durable memory is not implemented; no approved persistence design exists")
        kind = {
            MemoryOrigin.USER_STATED: EvidenceKind.KNOWN,
            MemoryOrigin.TOOL_VERIFIED: EvidenceKind.KNOWN,
            MemoryOrigin.MODEL_INFERRED: EvidenceKind.RECOMMENDED,
        }[origin]
        item = MemoryItem(
            key=key,
            value=value,
            origin=origin,
            scope=scope,
            recorded_at=now or _utcnow(),
            evidence_kind=kind,
            confidence=confidence,
        )
        with self._lock:
            self._facts[key] = item
            while len(self._facts) > MAX_SESSION_FACTS:
                # Oldest out. Bounded because the keys come from a conversation, and an
                # unbounded dict keyed by what a user says is growth a user controls.
                oldest = min(self._facts.values(), key=lambda entry: entry.recorded_at)
                del self._facts[oldest.key]
        return item

    def recall(self, key: str) -> Optional[MemoryItem]:
        with self._lock:
            return self._facts.get(key)

    def facts(self) -> tuple[MemoryItem, ...]:
        with self._lock:
            return tuple(sorted(self._facts.values(), key=lambda entry: entry.recorded_at))

    def forget(self, key: str) -> bool:
        """Delete one fact. Returns whether there was one."""
        with self._lock:
            return self._facts.pop(key, None) is not None

    # ------------------------------------------------------------------ summary

    def record_turn(
        self, intent: Intent, tools_used: tuple[str, ...], *, answered: bool, now: Optional[datetime.datetime] = None
    ) -> None:
        with self._lock:
            self._turns.append(TurnRecord(intent=intent, tools_used=tools_used, at=now or _utcnow(), answered=answered))

    def summary(self) -> tuple[str, ...]:
        """The rolling summary, oldest first. Deterministic text, safe to show or log."""
        with self._lock:
            return tuple(record.describe() for record in self._turns)

    def last_intent(self) -> Optional[Intent]:
        with self._lock:
            return self._turns[-1].intent if self._turns else None

    # ------------------------------------------------------------------ deletion

    def clear(self) -> None:
        """Forget everything about this conversation.

        What a user's "clear chat" maps onto. Total rather than selective: a partial clear
        that left inferences behind would be the kind of surprise this layer should not have.
        """
        with self._lock:
            self._facts.clear()
            self._turns.clear()

    def describe_retention(self) -> dict[str, str]:
        """What is held and how it goes away. Answers a user's data question directly."""
        return {
            "ephemeral_facts": f"{len(self.facts())} item(s), in memory only, cleared on clear() or worker restart",
            "rolling_summary": f"{len(self.summary())} turn(s), deterministic, in memory only",
            "turn_evidence": "discarded at the end of each turn; never carried forward",
            "durable_preferences": "not implemented; nothing is written to the database",
        }


class MemoryStore:
    """Session memories for this worker.

    Process-local, exactly like the VA-2 voice session registry, and with the same consequence:
    no cross-worker recovery. Recorded rather than worked around, because the fix is shared
    state and that is a deployment decision.
    """

    #: Sessions held before the least recently used is dropped.
    MAX_SESSIONS = 256

    def __init__(self) -> None:
        self._sessions: dict[str, SessionMemory] = {}
        self._order: deque = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _key(session_id: str, user_id: int) -> str:
        """The internal key, namespaced by the authenticated account.

        The session id arrives from the client, so two accounts can name the same one -- by
        accident, or because the route's default id is derived from the caller's own user id and
        is therefore guessable. Keying on the id alone meant the *first* account to claim a
        string owned it, and the second was refused with a 404: no leak, because the ownership
        check below did its job, but the rightful owner of a predictable default key could be
        locked out of their own conversation by anyone who guessed it. Namespacing makes the
        collision impossible rather than caught, which is the difference between an isolation
        check and an isolation property.
        """
        return f"{user_id}:{session_id}"

    def for_session(self, session_id: str, user_id: int) -> SessionMemory:
        """Get or create this session's memory.

        The `user_id` is both part of the key and checked against what was stored. The check is
        now unreachable from the route -- two accounts cannot produce one key -- and is kept
        deliberately: it is the invariant's last line, and a future caller that builds a key
        some other way would be caught by it rather than silently handed someone else's turn.
        """
        key = self._key(session_id, user_id)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                if existing.user_id != user_id:
                    raise PermissionError("session memory belongs to a different user")
                return existing
            created = SessionMemory(session_id=session_id, user_id=user_id)
            self._sessions[key] = created
            self._order.append(key)
            while len(self._order) > self.MAX_SESSIONS:
                self._sessions.pop(self._order.popleft(), None)
            return created

    def drop(self, session_id: str, user_id: int) -> None:
        with self._lock:
            self._sessions.pop(self._key(session_id, user_id), None)

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._order.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


#: Module-level, mirroring `app/voice/session_registry.py`'s singleton.
store = MemoryStore()


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
