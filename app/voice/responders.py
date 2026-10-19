"""The authenticated application layer that answers a spoken turn.

`TurnRuntime` takes an injected `respond` callable and deliberately knows nothing about where
the answer comes from -- that is what stops it growing a second opinion about grading or
grounding. This module is the other half of that seam: it builds a responder *from an identity
the gateway has already authenticated*, and it is the only file on the realtime path that is
allowed to import the orchestrator, the tool registry or the interview engine.

**Identity.** Every responder closes over a `User` row that came from the bearer token on the
WebSocket handshake. No message on the socket names a user, a profile or an interview owner, and
nothing here reads one. A transcript is the student's words, not an authorization claim -- a
model that said "look up user 42" would be describing, not permitting.

**Why the work runs in a worker thread.** Both responders are synchronous underneath: the
orchestrator and `interview_service` are ordinary blocking SQLAlchemy code. The gateway's event
loop is shared with every other live socket, so the whole responder body is handed to
`asyncio.to_thread`. The connection's single `Session` is therefore touched from a worker
thread, which is safe here for the reason the test harness already relies on: one turn runs at a
time per session (`TurnChannel` refuses a second concurrent commit), so the session is used
sequentially, never concurrently. `check_same_thread=False` is already set on the engine for the
same reason the sync routes need it.

**What is deliberately not here.** No ACTION tool, no confirmation flow, no second grader, no
second progression rule and no question generation of its own. The coach catalogue is built with
`include_actions` left at its default of False, so there is no mutating capability in it for the
orchestrator to reach -- the read-only guarantee is a property of the catalogue, not of
remembering to call the read-only entry point (it calls `invoke_read` as well; both).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.agent.coaching import CoachingReport, CoachingTurn
from app.agent.contracts import AgentRequest, Intent, TurnBudget
from app.agent.memory import store as memory_store
from app.agent.orchestrator import Orchestrator
from app.ai.provider import AIGenerationError, get_ai_provider
from app.models.conversation import Conversation
from app.models.user import User
from app.services import conversation_history, interview_service, voice_interview
from app.services.voice_interview import EvaluationAvailability, VoiceTurn, VoiceTurnState
from app.tools.contracts import ToolContext
from app.tools.registry import build_default_registry
from app.voice import conversation as voice_conversation
from app.voice.turn_runtime import Answer

logger = logging.getLogger(__name__)

#: Read-only and world-inclusive, exactly as `app/api/coach.py` builds it. Shared module-level
#: singletons for the same reason that file has them: the catalogue is immutable and building one
#: per connection would be pure cost.
_REGISTRY = build_default_registry()
_ORCHESTRATOR = Orchestrator(_REGISTRY)

#: How much of a coaching report may be read aloud. A full report can run to a dozen claims
#: across five evidence categories, which is a fine thing to read on a screen and about four
#: minutes of synthesis to sit through. Trimmed with an explicit note rather than silently, so
#: the student is never left thinking they heard everything.
MAX_SPOKEN_CHARACTERS = 1_200

_TRIMMED_NOTE = "I have left out some detail to keep this short."

#: One label per evidence category, spoken once before that category's claims. The partitioning
#: is `CoachingReport`'s, not re-decided here, so the spoken answer cannot disagree with the
#: grounding layer about which statements are facts and which are suggestions. Saying it out loud
#: matters more in speech than on screen: a screen shows the heading, a voice that ran the
#: categories together would present a recommendation in the same tone as a stored record.
_SECTION_LABELS = (
    ("stored_facts", "From your records:"),
    ("retrieved_observations", "From live sources:"),
    ("derived_analysis", "Working from that:"),
    ("recommendations", "What I suggest:"),
)

_NOTHING_TO_SAY = "I checked, and I do not have anything recorded to tell you about that yet."


# --------------------------------------------------------------------------- career coach


def _profile_id(db: Session, user: User) -> int:
    """This student's profile id, for scoping a conversation lookup.

    Imported locally, matching the convention already used further down this file: the service
    layer pulls in the ORM models, and importing it at module scope would make this module --
    which the WebSocket gateway imports on every connection -- pay for that graph up front.
    """
    from app.services.profile_service import get_or_create_profile

    return get_or_create_profile(db, user).id


def _recent_history(db: Session, *, profile_id: int, session_key: str):
    """The last few turns of this conversation, oldest first, as (role, text).

    Read through `conversation_history`, which is the store the gateway already persists every
    turn into -- so chat context and the student's visible history are the same records, not a
    second parallel notion of "the conversation".

    Ownership is re-derived here from `profile_id` rather than trusted: the conversation is
    looked up by (profile_id, session_key), so a session key alone cannot reach another
    student's transcript even if one were somehow guessed.

    Returns an empty tuple on any failure. Context is an enhancement -- a first turn has none
    by definition -- and a history read that fails must not take the reply down with it. Note
    the agent's own `SessionMemory` cannot serve this: it records turn *metadata* (intent,
    tools used, whether it was answered) and deliberately holds no transcript text.
    """
    try:
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.profile_id == profile_id,
                Conversation.session_key == session_key,
            )
            .one_or_none()
        )
        if conversation is None:
            return ()
        turns = conversation_history.get_turns(
            db, conversation_id=conversation.id, limit=voice_conversation.MAX_CONTEXT_TURNS * 2
        )
        return tuple((turn.role, turn.text) for turn in turns if turn.text)
    except Exception as exc:
        logger.warning("voice chat context unavailable (%s)", type(exc).__name__)
        return ()


def build_coach_responder(*, user: User, db: Session, session_key: str):
    """A responder bound to one authenticated student's coaching conversation.

    `session_key` scopes the bounded VA-6 session memory so follow-up turns on the same socket
    share context. It is derived from the session id the server minted, never from anything the
    client sent, so it cannot be used to reach another conversation -- and `MemoryStore` checks
    the owner anyway.
    """

    def answer_for(text: str) -> Answer:
        context = ToolContext(user=user, db=db)
        memory = memory_store.for_session(session_key, user_id=user.id)
        try:
            result = _ORCHESTRATOR.coach(
                AgentRequest(text=text), context, memory, budget=TurnBudget()
            )
        except Exception:
            # One `Session` serves the whole socket, unlike the HTTP routes which get a fresh one
            # per request. A failed statement leaves it needing a rollback, and without one every
            # later turn on this connection fails with PendingRollbackError -- a single bad turn
            # would take the rest of the conversation with it.
            db.rollback()
            raise
        report = result.report
        if report.intent is Intent.UNCLEAR and not report.failures:
            # `Intent.UNCLEAR` is the classifier saying this turn maps to none of its career
            # capabilities -- which is exactly what "hey, how are you?" or "explain what a
            # database is" is. Those used to land on a clarifying question or the fixed
            # "nothing recorded" sentence, which is why the coach appeared unable to hold a
            # conversation.
            #
            # The first version of this routed on *claim emptiness* instead, and that was
            # wrong in a way two existing tests caught. "What are my weaknesses?" on a fresh
            # account is `Intent.WEAKNESSES` with no claims -- a recognised career question
            # whose only honest answer is that nothing is recorded. Routing it to chat
            # replaced that honesty with a friendly non-answer, which is precisely what
            # `test_an_empty_account_says_so_rather_than_inventing_a_status` exists to forbid.
            #
            # So the discriminator is what the student *asked*, not what the records happened
            # to contain. Every recognised intent keeps the grounded path, empty-handed or
            # not. A reported failure also stays grounded: it is real information.
            spoken = voice_conversation.chat_reply(
                text,
                history=_recent_history(
                    db, profile_id=_profile_id(db, user), session_key=session_key
                ),
                provider=get_ai_provider(),
            )
            if spoken:
                # grounded=False, and that is the point: this sentence was written by a model,
                # not traced to a record. The flag is what keeps the two kinds of reply
                # distinguishable rather than letting model text pass as evidence.
                return Answer(text=spoken, tools_used=report.tools_used, grounded=False)
            # Fall through to the grounded renderer's own truthful "nothing to say" rather
            # than inventing a recovery line for a failed model call.

        return _speak_coaching(result)

    async def respond(text: str) -> Answer:
        return await asyncio.to_thread(answer_for, text)

    return respond


def _speak_coaching(result: CoachingTurn) -> Answer:
    """Render a coaching turn as something that can be said out loud.

    Nothing is added. Every sentence is either a claim the grounding layer produced, an
    uncertainty it recorded, a failure it already redacted, or one of the fixed labels above.
    There is no summarisation step, because a summary of evidence is a new claim about it.
    """
    report = result.report
    spoken: list[str] = []

    for attribute, label in _SECTION_LABELS:
        claims = getattr(report, attribute)
        if not claims:
            continue
        spoken.append(label)
        spoken.extend(claim.text for claim in claims)

    grounded = _has_claims(report)

    if report.unknowns:
        spoken.append("What I could not establish: " + "; ".join(report.unknowns))
    if report.failures:
        # Already redacted where they were raised. A world provider that is not configured --
        # which is the default, since no job provider is approved -- lands here as its own safe
        # message, so the student is told live market data is unavailable instead of being read
        # an invented vacancy.
        spoken.append("Some checks did not complete: " + "; ".join(report.failures))
    if report.clarifying_question:
        spoken.append(report.clarifying_question)

    if not spoken:
        # `GroundedAnswer.is_grounded` and the orchestrator's own fallback make this very hard to
        # reach, but silence presented as a reply is the one outcome worth a belt and braces:
        # an empty answer would come back as RESPONSE_FAILED, which is not what happened.
        return Answer(text=_NOTHING_TO_SAY, tools_used=report.tools_used, grounded=False)

    text = _fit(spoken)
    return Answer(text=text, tools_used=report.tools_used, grounded=grounded)


def _has_claims(report: CoachingReport) -> bool:
    return bool(
        report.stored_facts
        or report.retrieved_observations
        or report.derived_analysis
        or report.recommendations
    )


def _fit(parts: list[str]) -> str:
    """Join sentences up to the spoken budget, saying so if anything was dropped."""
    kept: list[str] = []
    used = 0
    for part in parts:
        if used + len(part) + 1 > MAX_SPOKEN_CHARACTERS:
            kept.append(_TRIMMED_NOTE)
            break
        kept.append(part)
        used += len(part) + 1
    return " ".join(kept)


# --------------------------------------------------------------------------- interview coach


class InterviewUnavailable(RuntimeError):
    """The interview context this session was opened for cannot be used.

    Raised at handshake time only. Mid-session the equivalent condition becomes an honest spoken
    answer instead, because by then the student is in a conversation and dropping the socket
    would tell them less than a sentence would.
    """


class InterviewResponder:
    """Answers each turn by submitting it to the existing authoritative interview engine.

    **Which question a turn answers** is held here, starting from the id validated at handshake
    and advancing only to the `next_question` the engine itself returned. The client never names
    a question id on the socket, so it cannot redirect an answer at a different question, and the
    pointer cannot run ahead of authoritative state because authoritative state is the only thing
    that moves it.

    **Replay cannot advance the interview twice.** That guarantee is the engine's, not this
    class's: a question takes one answer, enforced by a unique index, and `submit_answer`
    recognises a byte-identical resend as a replay. A resend therefore comes back as
    `ANSWER_REPLAYED` with the *same* pending next question, so advancing the pointer to it is
    idempotent -- it was already there.
    """

    def __init__(
        self,
        *,
        db: Session,
        profile_id: int,
        interview_id: int,
        first_question_id: int,
        ai_provider: Any,
    ) -> None:
        self._db = db
        self._profile_id = profile_id
        self._interview_id = interview_id
        self._question_id: Optional[int] = first_question_id
        self._ai_provider = ai_provider
        self._finished = False
        #: Set when an answer was stored but the question after it could not be generated. The
        #: next turn then retries *question generation* rather than re-submitting an answer the
        #: server already holds -- the student cannot be answering a question nobody read out.
        self._owed_question = False

    @property
    def current_question_id(self) -> Optional[int]:
        return self._question_id

    async def __call__(self, text: str) -> Answer:
        return await asyncio.to_thread(self._answer_for, text)

    def _answer_for(self, text: str) -> Answer:
        if self._finished or self._question_id is None:
            return Answer(
                text="This interview is finished. There are no further questions to answer.",
                grounded=True,
            )

        if self._owed_question:
            return self._retry_next_question()

        question_id = self._question_id
        try:
            turn = voice_interview.submit_spoken_turn(
                self._db,
                self._profile_id,
                self._interview_id,
                question_id,
                text,
                self._ai_provider,
            )
        except interview_service.InterviewNotFoundError:
            # The interview or the question is gone, or never belonged to this student. Ownership
            # was checked when the socket was accepted, so reaching here means the row changed
            # underneath a live session -- a deletion from another device, most likely.
            logger.info("voice interview %s is no longer available", self._interview_id)
            self._rollback()
            self._finished = True
            return Answer(
                text="This interview is no longer available on your account.",
                grounded=False,
            )
        except ValueError:
            # An empty or oversized transcript. Unreachable in practice: `TurnRuntime` refuses an
            # empty recognition before it calls a responder and applies the same 5 000-character
            # ceiling. Answered rather than raised so a future caller cannot turn it into an
            # opaque RESPONSE_FAILED.
            self._rollback()
            return Answer(
                text="I could not use that as an answer. Please try saying it again.",
                grounded=False,
            )
        except Exception:
            self._rollback()
            raise

        return self._speak(turn)

    def _rollback(self) -> None:
        """Clear a failed statement off the connection's one `Session`.

        The HTTP routes get a fresh session per request and never need this. A socket holds one
        for the whole conversation, so an un-rolled-back failure turns every later turn into a
        `PendingRollbackError` -- one bad turn would end the interview.
        """
        try:
            self._db.rollback()
        except Exception:  # noqa: BLE001 - a session too broken to roll back is already lost
            logger.warning("could not roll back the voice interview session")

    def _retry_next_question(self) -> Answer:
        """Ask the engine for the question it could not produce last turn.

        Only question generation is retried. The answer behind it is already durable, and
        re-submitting it is what the engine would have to refuse -- this is the "reconcile
        authoritative state before retrying" rule applied to the one uncertain step on this path.
        """
        try:
            following = interview_service.get_next_question(
                self._db, self._profile_id, self._interview_id, self._ai_provider
            )
        except (AIGenerationError, interview_service.InterviewNotFoundError):
            self._rollback()
            return Answer(
                text="Your answer is still saved. I still cannot prepare the next question.",
                grounded=False,
            )

        self._owed_question = False
        if following.completed:
            self._question_id = None
            self._finished = True
            return Answer(
                text="Your answer is saved and the interview is complete.", grounded=True
            )
        if following.question is None:
            self._owed_question = True
            return Answer(
                text="Your answer is still saved. I do not have the next question to read out.",
                grounded=False,
            )
        self._question_id = following.question.id
        return Answer(text=_question_sentence(following), grounded=True)

    def _speak(self, turn: VoiceTurn) -> Answer:
        if turn.state is VoiceTurnState.ANSWER_REFUSED:
            # Authoritative and final: the first answer to a question wins. Reported as a fact
            # rather than a limitation, and the pointer stays put -- offering the next question
            # here would imply the interview moved, which it did not.
            return Answer(
                text=(
                    "That question already has a different answer recorded, and the first answer "
                    "is final. Nothing was changed."
                ),
                grounded=True,
            )

        if turn.state is VoiceTurnState.NEXT_QUESTION_UNAVAILABLE:
            # The answer is durable. Saying so explicitly is what stops a client retrying a turn
            # the server already holds, which the engine would then have to refuse. The pointer
            # deliberately does not move -- the next question does not exist yet -- and the next
            # turn retries only the generation, through `_retry_next_question`.
            self._owed_question = True
            return Answer(
                text="Your answer is saved. I could not prepare the next question just now.",
                grounded=False,
            )

        if turn.state is VoiceTurnState.INTERVIEW_COMPLETE:
            self._question_id = None
            self._finished = True
            closing = "That was the last question. Your answer is saved and the interview is complete."
            if turn.evaluation is EvaluationAvailability.READY:
                closing += " Your evaluation is ready."
            else:
                # NOT_READY or UNKNOWN. Neither is "ready", and reading them the same would turn
                # "we could not check" into a promise.
                closing += " Your evaluation is not ready yet."
            return Answer(text=closing, grounded=True)

        # ANSWER_ACCEPTED or ANSWER_REPLAYED, both with a question attached.
        following = turn.next_question
        if following is None or following.question is None:
            # The engine reported progress without a question. Not inventing one.
            logger.warning("interview %s advanced without a question", self._interview_id)
            return Answer(
                text="Your answer is saved. I do not have the next question to read out.",
                grounded=False,
            )

        self._question_id = following.question.id
        prefix = "That answer was already recorded. " if turn.state is VoiceTurnState.ANSWER_REPLAYED else ""
        return Answer(text=prefix + _question_sentence(following), grounded=True)


def _question_sentence(following: Any) -> str:
    """Read a question out with its position, when the engine supplied one.

    The counter comes from `NextInterviewQuestionResponse`, which derives it server-side from the
    interview's own type and the question's order index. Counting questions here instead would be
    a second copy of a backend constant -- the exact mistake the Android screen already made once.
    """
    counter = ""
    if following.question_number is not None and following.total_questions is not None:
        counter = f"Question {following.question_number} of {following.total_questions}. "
    return f"{counter}{following.question.question_text}"


def build_interview_responder(
    *,
    user: User,
    db: Session,
    interview_id: int,
    interview_question_id: int,
    ai_provider: Any,
) -> InterviewResponder:
    """Validate the interview context against the authenticated owner, then bind a responder.

    The ownership check runs **before the socket is accepted**, through the engine's own paired
    check rather than a new one, so a session can never be established against someone else's
    interview and then discover it one turn later.
    """
    from app.services.profile_service import get_or_create_profile

    profile = get_or_create_profile(db, user)
    try:
        interview_service.owned_question(db, profile.id, interview_id, interview_question_id)
    except interview_service.InterviewNotFoundError as exc:
        # One answer for an unknown interview, an unknown question and someone else's, so an id
        # cannot be confirmed by probing a socket any more than by probing the HTTP route.
        raise InterviewUnavailable(str(exc)) from exc

    return InterviewResponder(
        db=db,
        profile_id=profile.id,
        interview_id=interview_id,
        first_question_id=interview_question_id,
        ai_provider=ai_provider,
    )
