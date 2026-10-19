"""Agent contracts: evidence, claims, answers, budgets.

Framework-free and vendor-free. No FastAPI, no SQLAlchemy, no model SDK, no transport. The
orchestrator imports the Tool Registry and the World layer; nothing here imports either, so the
contracts can be reasoned about on their own.

The central type is `Claim`. It exists to make one failure impossible: a sentence that sounds
like a fact about the student or the market but is traceable to nothing. A `Claim` without
evidence raises at construction, so "ground your answers" is a property of the code rather than
an instruction in a prompt that a model may or may not follow.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EvidenceKind(str, Enum):
    """How strongly a piece of evidence supports what is said with it.

    These are the states the specifications already use, and keeping them distinct is the
    point: "your resume says Python" and "adverts in Pune mention Python" and "you should learn
    Kubernetes" are three different kinds of statement, and collapsing them is how a
    recommendation starts sounding like a measurement.
    """

    #: A verified fact from the student's own authenticated records.
    KNOWN = "known"
    #: Observed from an external source during this turn, with provenance and freshness.
    LIVE_OBSERVED = "live_observed"
    #: Computed from one of the above -- a count, a gap, a comparison. Carries its basis.
    DERIVED = "derived"
    #: A suggestion. Never a fact, never storable as one.
    RECOMMENDED = "recommended"
    #: We looked and cannot say. The honest answer, and a first-class one.
    UNKNOWN = "unknown"


class Intent(str, Enum):
    """What the request is asking for.

    A closed set, deliberately. An open-ended intent space would mean a model choosing freely
    among capabilities, and VA-6's tool policy requires the opposite: a fixed mapping from
    intent to a small group of approved read tools, so what can run is knowable in advance.
    """

    PROFILE_STATUS = "profile_status"
    SKILL_STATUS = "skill_status"
    READINESS = "readiness"
    WEAKNESSES = "weaknesses"
    INTERVIEW_HISTORY = "interview_history"
    PROGRESS = "progress"
    RESUME_STATUS = "resume_status"
    SAVED_JOBS = "saved_jobs"
    TODAY_PLAN = "today_plan"
    #: World-facing
    JOB_SEARCH = "job_search"
    FRESHER_JOBS = "fresher_jobs"
    ROLE_REQUIREMENTS = "role_requirements"
    SKILL_DEMAND = "skill_demand"
    COMPANY_HIRING = "company_hiring"
    LOCATION_COMPARISON = "location_comparison"
    #: Needs both sides joined.
    SKILL_GAP_VS_MARKET = "skill_gap_vs_market"

    # VA-8 coaching intents. Both are joins over records the student already owns, which is
    # why they are personal-first: a role is chosen against recorded goals, skills and
    # readiness, and the market is consulted only to check the choice, never to make it.
    ROLE_SELECTION = "role_selection"
    CAREER_PLAN = "career_plan"
    #: Nothing matched. Not an error: it is the state that produces a clarifying question.
    UNCLEAR = "unclear"
    #: Recognised, and recognised as something this phase will not do.
    OUT_OF_SCOPE = "out_of_scope"


#: Intents whose answer needs a world retrieval. Separated because a world call spends a
#: provider budget and can fail in ways a database query cannot.
WORLD_INTENTS = frozenset(
    {
        Intent.JOB_SEARCH,
        Intent.FRESHER_JOBS,
        Intent.ROLE_REQUIREMENTS,
        Intent.SKILL_DEMAND,
        Intent.COMPANY_HIRING,
        Intent.LOCATION_COMPARISON,
        Intent.SKILL_GAP_VS_MARKET,
        # A plan consults the market for what a role asks for; role selection does not, because
        # which goals the student recorded is answerable from their own records alone.
        Intent.CAREER_PLAN,
    }
)


@dataclass(frozen=True)
class Entities:
    """What was extracted from the request, bounded and typed.

    Only the fields a world query or a tool argument can actually use. An entity bag that could
    hold anything would become a way to smuggle a parameter into a tool call.
    """

    role: Optional[str] = None
    location: Optional[str] = None
    locations: tuple[str, ...] = ()
    skill: Optional[str] = None
    experience_level: Optional[str] = None
    #: VA-8. The plan horizon the student asked for, in days, restricted to the four the
    #: product supports. Not free-form: "a 45-day plan" has no defined milestone shape here,
    #: and inventing one would produce a schedule nobody designed.
    horizon_days: Optional[int] = None
    #: VA-8. Hours per week the student said they have. Used to say when a plan does not fit
    #: rather than to silently shorten it -- a plan that quietly drops milestones to fit the
    #: budget would read as achievable when it is not the plan that was analysed.
    weekly_hours: Optional[int] = None

    MAX_TEXT = 120
    HORIZONS = (7, 30, 60, 90)
    MAX_WEEKLY_HOURS = 80

    def __post_init__(self) -> None:
        for name in ("role", "location", "skill", "experience_level"):
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, value.strip()[: self.MAX_TEXT] or None)
        object.__setattr__(
            self, "locations", tuple(loc.strip()[: self.MAX_TEXT] for loc in self.locations if loc.strip())[:3]
        )
        if self.horizon_days is not None and self.horizon_days not in self.HORIZONS:
            raise ValueError(f"horizon_days must be one of {self.HORIZONS}, not {self.horizon_days}")
        if self.weekly_hours is not None and not 1 <= self.weekly_hours <= self.MAX_WEEKLY_HOURS:
            raise ValueError("weekly_hours must be between 1 and 80")


@dataclass(frozen=True)
class EvidenceRef:
    """One traceable piece of support for a claim.

    `tool` and `summary` are always present; `provider`, `retrieved_at` and `freshness` are set
    for world evidence and absent for personal evidence, because a database row about the
    student has no provider and no freshness policy. Carrying them as Optional rather than
    inventing values keeps the difference visible.
    """

    kind: EvidenceKind
    #: The registered capability that produced it, or "none" for an explicit unknown.
    tool: str
    #: A short, already-redacted description. Never raw external prose.
    summary: str
    provider: Optional[str] = None
    retrieved_at: Optional[datetime.datetime] = None
    published_at: Optional[datetime.datetime] = None
    freshness: Optional[str] = None
    #: Required for DERIVED world evidence: the sample it was computed over.
    sample: Optional[str] = None
    location: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.tool:
            raise ValueError("evidence must name the tool that produced it")
        if self.kind is EvidenceKind.LIVE_OBSERVED and not self.provider:
            raise ValueError("live-observed evidence must name its provider")
        if self.kind is EvidenceKind.LIVE_OBSERVED and self.retrieved_at is None:
            raise ValueError("live-observed evidence must say when it was retrieved")

    def describe(self) -> str:
        parts = [self.summary]
        if self.provider:
            parts.append(f"[{self.provider}")
            if self.freshness:
                parts[-1] += f", {self.freshness}"
            parts[-1] += "]"
        return " ".join(parts)


@dataclass(frozen=True)
class Claim:
    """One statement, and what supports it.

    The invariant: no evidence, no claim. This is why the orchestrator cannot emit an
    ungrounded sentence even if a model hands it one -- there is nowhere to put it.
    """

    text: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a claim needs text")
        if not self.evidence:
            raise ValueError(f"claim {self.text[:40]!r} has no evidence; an ungrounded claim cannot be made")
        # A claim may be supported by several kinds, but the weakest one governs how it should
        # be read, so it is computed rather than asserted (see `weakest_kind`).

    @property
    def weakest_kind(self) -> EvidenceKind:
        order = [
            EvidenceKind.UNKNOWN,
            EvidenceKind.RECOMMENDED,
            EvidenceKind.DERIVED,
            EvidenceKind.LIVE_OBSERVED,
            EvidenceKind.KNOWN,
        ]
        return min((ref.kind for ref in self.evidence), key=order.index)


@dataclass(frozen=True)
class GroundedAnswer:
    """What a turn produces.

    `claims` is the substance; `uncertainties` is what could not be established. Both can be
    non-empty at once, which is the usual case and the reason they are separate fields: an
    answer that reported only what it knew would read as complete.
    """

    intent: Intent
    claims: tuple[Claim, ...] = ()
    uncertainties: tuple[str, ...] = ()
    #: Capabilities actually executed this turn, in order. The audit trail.
    tools_used: tuple[str, ...] = ()
    #: A clarifying question, when the request could not be understood well enough to act.
    clarifying_question: Optional[str] = None
    #: Set when a provider or tool failed, already redacted.
    failures: tuple[str, ...] = ()

    @property
    def is_grounded(self) -> bool:
        """True when there is at least one supported claim, or an honest nothing.

        An answer with no claims and no uncertainties and no question would be silence
        presented as a response, which is the one shape this type should never allow to look
        successful.
        """
        return bool(self.claims or self.uncertainties or self.clarifying_question)

    def evidence_kinds(self) -> frozenset[EvidenceKind]:
        return frozenset(ref.kind for claim in self.claims for ref in claim.evidence)


# --------------------------------------------------------------------------- budget


class AgentError(Exception):
    """Base for orchestration failures. Carries a code so callers branch on that, not text."""

    code = "agent_error"

    def safe_message(self) -> str:
        return str(self)


class TurnCancelled(AgentError):
    code = "cancelled"


class TurnTimeout(AgentError):
    code = "timeout"

    def __init__(self, elapsed: float, budget: float) -> None:
        super().__init__(f"the turn exceeded its {budget:.3g}s budget after {elapsed:.3g}s")


class TurnLimitReached(AgentError):
    """Bounded iteration. Hit the ceiling without converging.

    A distinct failure rather than "just answer with what you have": a loop that quietly
    stopped early and presented a partial result as complete is worse than one that says it
    ran out of steps.
    """

    code = "iteration_limit"

    def __init__(self, limit: int) -> None:
        super().__init__(f"the turn reached its limit of {limit} tool round(s) without resolving")


class ModelUnavailable(AgentError):
    """No configured provider can do what was asked of a model.

    Not fatal by itself: the deterministic path handles every intent, so this surfaces as a
    limitation on the answer rather than as a failed turn.
    """

    code = "model_unavailable"


@dataclass
class TurnBudget:
    """Bounds on one turn: wall clock, tool calls, tool rounds.

    Three limits rather than one because they fail differently. A deadline catches a slow
    provider; a call ceiling catches a plan that wants to fan out; a round ceiling catches a
    loop that keeps deciding it needs one more look.
    """

    seconds: float = 25.0
    max_tool_calls: int = 6
    max_rounds: int = 3
    _started: float = field(default_factory=time.monotonic, init=False)
    _calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.seconds <= 0 or self.max_tool_calls <= 0 or self.max_rounds <= 0:
            raise ValueError("every turn bound must be positive")

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def calls_made(self) -> int:
        return self._calls

    @property
    def calls_remaining(self) -> int:
        return max(0, self.max_tool_calls - self._calls)

    def check(self) -> None:
        if self.elapsed_seconds > self.seconds:
            raise TurnTimeout(self.elapsed_seconds, self.seconds)

    def spend_call(self) -> None:
        """Count one tool call, or raise.

        Counted before the call, not after: a call that times out still consumed the budget,
        and counting only what returned is how a failing provider gets retried six times.
        """
        self.check()
        if self._calls >= self.max_tool_calls:
            raise TurnLimitReached(self.max_tool_calls)
        self._calls += 1


@dataclass(frozen=True)
class AgentRequest:
    """One turn's input.

    `text` is whatever the user said. It is treated as untrusted for prompt purposes and is
    never used to name a tool directly -- the intent classifier maps it onto a closed set, and
    only that set reaches the registry.
    """

    text: str
    #: Set when the caller already knows the intent (a UI button rather than free speech).
    intent_hint: Optional[Intent] = None
    MAX_TEXT = 2_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", self.text.strip()[: self.MAX_TEXT])


@dataclass(frozen=True)
class AgentTurn:
    """What the orchestrator returns: the answer plus how it got there."""

    request_text: str
    intent: Intent
    entities: Entities
    answer: GroundedAnswer
    duration_seconds: float
    tool_calls: int
    rounds: int
    used_model: bool = False

    def to_payload(self) -> dict[str, Any]:
        """A transport-neutral dict. No vendor shape, no ORM object, no untrusted prose."""
        return {
            "intent": self.intent.value,
            "claims": [
                {
                    "text": claim.text,
                    "kind": claim.weakest_kind.value,
                    "evidence": [ref.describe() for ref in claim.evidence],
                }
                for claim in self.answer.claims
            ],
            "uncertainties": list(self.answer.uncertainties),
            "clarifying_question": self.answer.clarifying_question,
            "failures": list(self.answer.failures),
            "tools_used": list(self.answer.tools_used),
            "used_model": self.used_model,
        }
