"""VA-9: the voice-interview turn, composed from the existing interview engine.

**What this adds, and what it deliberately does not.** It adds one thing: the composition of
"store this spoken answer" and "give me the next question" into a single authoritative turn,
with every outcome of that composition named. It adds no scoring, no evaluation, no question
generation, no progression rule and no second interview state machine. Every one of those stays
in `interview_service`, which remains the only owner of interview state.

**Why the transcript goes through the text path.** `interview_service.submit_answer` compares
the submitted text against the stored answer, so a resend is recognised byte for byte. The
audio path cannot do that -- the recording is not persisted, so its replay identity is only
"this question's stored answer also came from audio", which its own docstring calls the honest
limit of what existing state can prove. A realtime voice turn already has a transcript by the
time it reaches the server, so using the text path gives the *stronger* guarantee rather than
inheriting the weaker one. The answer is still marked as what it is by the caller's choice of
path, and `transcribed` stays false because this is a text submission of text the client
produced -- the same thing a typed answer is, as far as replay identity is concerned.

**Why there is no new turn token.** `interview_question_id` is the turn token. A late result
from an earlier question carries that earlier question's id, so it is submitted against the
question it actually belongs to, where the engine's own guard answers it -- as a replay if it is
the same answer, as a refusal if it is a different one. A separate token would be a second
mechanism that could disagree with the first.

**Why the answer is never lost when the next question fails.** The two steps are not atomic and
must not pretend to be: the answer is durable the moment `submit_answer` returns, and question
generation is an AI call that can fail afterwards. So a failure there is reported as
`NEXT_QUESTION_UNAVAILABLE` *with* the stored answer, never as a failed turn -- a client that
retried the whole turn on a generic error would be re-submitting an answer that is already
stored, which the engine would then have to refuse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.ai.provider import AIGenerationError
from app.schemas.interview import NextInterviewQuestionResponse
from app.services import interview_service

logger = logging.getLogger(__name__)

#: The same ceiling `SubmitInterviewAnswerRequest.answer_text` already enforces, taken from it
#: rather than chosen again. A voice path that accepted a longer answer than the typed path
#: would mean the two modes disagree about what a valid answer is, and the grader would be
#: reading something the text route would have refused. Refused rather than truncated: silently
#: cutting an answer would change what the student said before it was graded.
MAX_TRANSCRIPT_CHARACTERS = 5_000


class VoiceTurnState(str, Enum):
    """Every outcome of one spoken turn, named.

    The three that would otherwise collapse into "error" are the ones that matter:
    `ANSWER_REPLAYED` is a success the client must not treat as a new answer,
    `ANSWER_REFUSED` means this question already has a *different* answer and retrying cannot
    help, and `NEXT_QUESTION_UNAVAILABLE` means the answer is safely stored and only the
    question after it is missing.
    """

    #: Answer stored for the first time, and a next question is attached.
    ANSWER_ACCEPTED = "answer_accepted"
    #: The same answer was already stored. Idempotent success, nothing advanced twice.
    ANSWER_REPLAYED = "answer_replayed"
    #: This question already holds a different answer. The first one is final.
    ANSWER_REFUSED = "answer_refused"
    #: Answer stored and the interview has no further questions.
    INTERVIEW_COMPLETE = "interview_complete"
    #: Answer stored; the next question could not be generated. Do not resubmit the answer.
    NEXT_QUESTION_UNAVAILABLE = "next_question_unavailable"


class EvaluationAvailability(str, Enum):
    """What can honestly be said about the evaluation right now.

    Separate from the turn state because they answer different questions, and separate from a
    boolean because "not yet" and "we tried and could not" must not read the same.
    """

    NOT_READY = "not_ready"  # the interview is not complete, so there is nothing to grade
    READY = "ready"  # complete: the existing evaluation endpoint can be called
    UNKNOWN = "unknown"  # the interview's own state could not be read back


@dataclass(frozen=True)
class VoiceTurn:
    """The authoritative result of one spoken turn.

    `stored_answer_text` is what the server actually holds, read back rather than echoed from
    the request: on a replay those differ in principle, and the stored one is the one that will
    be graded.
    """

    state: VoiceTurnState
    interview_id: int
    answered_question_id: int
    stored_answer_text: Optional[str]
    next_question: Optional[NextInterviewQuestionResponse]
    evaluation: EvaluationAvailability
    #: Safe, redacted, and never a provider's or database's own words.
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.state is VoiceTurnState.ANSWER_REFUSED and self.next_question is not None:
            # A refused answer must not come with a question that looks like progress.
            raise ValueError("a refused answer cannot advance the interview")
        if self.state in (VoiceTurnState.ANSWER_ACCEPTED, VoiceTurnState.ANSWER_REPLAYED) and not self.stored_answer_text:
            raise ValueError("an accepted or replayed answer must report what is stored")

    @property
    def answer_is_stored(self) -> bool:
        """True whenever the server holds this turn's answer -- including the two states a
        naive client would read as failure. This is the flag that decides whether resubmitting
        is safe, and it is deliberately not derived from `state == ANSWER_ACCEPTED`."""
        return self.state in (
            VoiceTurnState.ANSWER_ACCEPTED,
            VoiceTurnState.ANSWER_REPLAYED,
            VoiceTurnState.INTERVIEW_COMPLETE,
            VoiceTurnState.NEXT_QUESTION_UNAVAILABLE,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "interview_id": self.interview_id,
            "answered_question_id": self.answered_question_id,
            "answer_is_stored": self.answer_is_stored,
            "stored_answer_text": self.stored_answer_text,
            "next_question": self.next_question.model_dump(mode="json") if self.next_question else None,
            "evaluation": self.evaluation.value,
            "detail": self.detail,
        }


def _evaluation_state(db: Session, profile_id: int, interview_id: int) -> EvaluationAvailability:
    """Whether the interview is complete, from the interview row itself.

    Read from authoritative state rather than inferred from "the last question was answered":
    completion is the engine's decision, and a client that counted questions would be holding
    a second, disagreeing copy of it.
    """
    try:
        interview = interview_service.read_interview(db, profile_id, interview_id)
    except Exception:  # noqa: BLE001 - any read failure is reported as unknown, never as ready
        logger.info("could not read interview %s back for its evaluation state", interview_id)
        return EvaluationAvailability.UNKNOWN
    if interview is None:
        return EvaluationAvailability.UNKNOWN
    return (
        EvaluationAvailability.READY
        if interview.status == "completed"
        else EvaluationAvailability.NOT_READY
    )


def submit_spoken_turn(
    db: Session,
    profile_id: int,
    interview_id: int,
    interview_question_id: int,
    transcript: str,
    ai_provider: Any,
) -> VoiceTurn:
    """Store one spoken answer and return the next question, or say exactly why not.

    Raises only `interview_service.InterviewNotFoundError`, which the route maps to a 404 -- an
    unknown interview, an unknown question, or one belonging to someone else, all answered
    identically so an id cannot be confirmed by probing. Every other outcome is a state.
    """
    text = (transcript or "").strip()
    if not text:
        # Refused here rather than stored. An empty answer is almost always a recogniser that
        # heard nothing, and storing it would spend the student's one answer for this question
        # on silence -- irreversible, because the first answer is final.
        raise ValueError("a spoken answer cannot be empty")
    if len(text) > MAX_TRANSCRIPT_CHARACTERS:
        raise ValueError(f"a spoken answer cannot exceed {MAX_TRANSCRIPT_CHARACTERS} characters")

    # Ownership first, through the engine's own paired check, so a question belonging to
    # another interview never reaches the answer path.
    interview_service.owned_question(db, profile_id, interview_id, interview_question_id)

    # Read before writing, purely to tell a first submission from a resend. `submit_answer`
    # answers both with success -- correctly, since the student is owed the result either way --
    # so the distinction is not recoverable from its return value, and the client needs it to
    # know whether this turn advanced anything.
    already_stored = interview_service.answer_text_for(db, interview_question_id)

    try:
        stored = interview_service.submit_answer(
            db, profile_id, interview_id, interview_question_id, text
        )
    except interview_service.InterviewQuestionNotPendingError as exc:
        # A different answer is already final for this question. Reported with no next question
        # at all, because nothing advanced and offering one would imply it did.
        logger.info("voice turn refused for question %s: already answered", interview_question_id)
        return VoiceTurn(
            state=VoiceTurnState.ANSWER_REFUSED,
            interview_id=interview_id,
            answered_question_id=interview_question_id,
            stored_answer_text=already_stored,
            next_question=None,
            evaluation=_evaluation_state(db, profile_id, interview_id),
            detail=str(exc),
        )

    replayed = already_stored is not None

    try:
        following = interview_service.get_next_question(db, profile_id, interview_id, ai_provider)
    except AIGenerationError:
        # The answer is already durable. Reporting a failed turn here would invite a client to
        # resubmit an answer the server holds, which the engine would then have to refuse.
        logger.info("voice turn stored its answer but could not generate the next question")
        return VoiceTurn(
            state=VoiceTurnState.NEXT_QUESTION_UNAVAILABLE,
            interview_id=interview_id,
            answered_question_id=interview_question_id,
            stored_answer_text=stored.answer_text,
            next_question=None,
            evaluation=_evaluation_state(db, profile_id, interview_id),
            detail="Your answer was saved. The next question could not be prepared just now.",
        )

    evaluation = _evaluation_state(db, profile_id, interview_id)
    if following.completed:
        state = VoiceTurnState.INTERVIEW_COMPLETE
    elif replayed:
        state = VoiceTurnState.ANSWER_REPLAYED
    else:
        state = VoiceTurnState.ANSWER_ACCEPTED

    # Identity and ids only -- never the transcript, which is the student's own words about
    # themselves and runs through this line on every turn.
    logger.info(
        "voice turn profile=%s interview=%s question=%s state=%s",
        profile_id,
        interview_id,
        interview_question_id,
        state.value,
    )
    return VoiceTurn(
        state=state,
        interview_id=interview_id,
        answered_question_id=interview_question_id,
        stored_answer_text=stored.answer_text,
        next_question=None if following.completed else following,
        evaluation=evaluation,
        detail="This answer was already recorded." if replayed else None,
    )
