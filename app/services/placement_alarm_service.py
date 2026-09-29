import logging
import random
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
from app.ai.output_guard import contains_prompt_leak
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.assessment import GeneratedQuestion
from app.models.placement_alarm_challenge import PlacementAlarmChallenge
from app.models.placement_alarm_question_attempt import PlacementAlarmQuestionAttempt
from app.models.question import Question
from app.schemas.placement_alarm import ChallengeAnswerResult, ChallengeQuestionRead, ChallengeStateRead
from app.services.ai_boundary import outside_transaction
from app.services.skill_matching import NoResumeAnalysisError, is_placement_topic, latest_resume_skills

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "alarm_mcq_generator_v2.txt"

# Fixed product rule -- never a user setting, never overridden. The alarm always presents
# exactly this many questions regardless of how many distinct skills the student has.
REQUIRED_QUESTIONS = 6

logger = logging.getLogger(__name__)


def _sanitize_generated_question(generated: GeneratedQuestion, skill: str) -> GeneratedQuestion:
    """Code-level backstop -- see app/ai/output_guard.py's module docstring. Mirrors
    assessment_run_service._sanitize_generated_question (kept as separate, independent code
    per this feature's own module, not a shared call, matching this codebase's existing
    per-feature duplication pattern for this class of prompt/service pairing).

    Checked over the whole object rather than two named fields, which closes two real gaps this
    enumeration had: `options` (the four choices, persisted and rendered verbatim) and
    `expected_answer_or_rubric`, which submit_answer returns to the student as `correct_answer`
    the moment they get the question wrong. Both were generated text that no check ever read."""
    if contains_prompt_leak(generated):
        logger.warning(
            "Discarding a generated alarm question for skill=%r: output contained prompt-leak markers.",
            skill,
        )
        return GeneratedQuestion(
            type="mcq",
            prompt=f"Which of the following best relates to {skill!r}?",
            options=["A relevant concept", "An unrelated concept", "A different unrelated concept", "None of these"],
            expected_answer_or_rubric="A relevant concept",
            explanation="This question checks general understanding of the topic.",
        )
    return generated


class ChallengeNotFoundError(Exception):
    """Challenge not found, or not owned by this profile."""


class QuestionNotInChallengeError(Exception):
    """No attempt row for this question id within this challenge."""


class QuestionAlreadyCorrectError(Exception):
    """This question slot was already answered correctly -- no re-submitting a solved
    question."""


class ChallengeGenerationError(Exception):
    """Question generation failed partway through -- the caller should ask the student to
    retry rather than being left with a permanently stuck challenge."""


def _placement_relevant(skills: List[str]) -> List[str]:
    return [s for s in skills if is_placement_topic(s)]


def _assign_six(pool: List[str]) -> List[str]:
    """Pure distribution logic, kept separate from the DB lookup so it's directly unit-
    testable: given a non-empty pool of eligible skills, returns exactly REQUIRED_QUESTIONS
    skill assignments, one per question slot. The pool size does not need to equal
    REQUIRED_QUESTIONS -- fewer than REQUIRED_QUESTIONS means some skills repeat across
    slots (e.g. a single skill fills all 6); REQUIRED_QUESTIONS or more means that many
    distinct skills, one per slot, no repeats. Never invents a skill outside the pool --
    repetition, not fabrication, fills the count. The product requirement is 6 questions,
    not 6 skills."""
    if len(pool) >= REQUIRED_QUESTIONS:
        return random.sample(pool, REQUIRED_QUESTIONS)

    shuffled = pool[:]
    random.shuffle(shuffled)
    return [shuffled[i % len(shuffled)] for i in range(REQUIRED_QUESTIONS)]


def has_eligible_skills(db: Session, profile_id: int) -> bool:
    """Cheap eligibility check for the Android alarm-create screen -- reuses the exact same
    resume lookup + placement-topic allowlist filter as select_alarm_skills, but never touches
    the AI provider (no question generation, no rate limit needed). Lets the client proactively
    show "complete your resume to enable Placement Alarm" before the student tries to save an
    alarm, rather than only surfacing it as a failed challenge-start at 7am."""
    try:
        pool = _placement_relevant(latest_resume_skills(db, profile_id))
    except NoResumeAnalysisError:
        return False
    return bool(pool)


def select_alarm_skills(db: Session, profile_id: int) -> List[str]:
    """Fetches the student's actual resume-derived skills, filters them down to only the
    recognized placement/interview-prep technical & aptitude topics (an allowlist -- a resume
    skill that isn't a recognized placement topic is excluded even though it's real), and hands
    the resulting pool to _assign_six. Propagates NoResumeAnalysisError unchanged (no completed
    resume yet, or nothing placement-relevant survived the filter) so the API layer can turn it
    into a single "complete your resume first" signal."""
    pool = _placement_relevant(latest_resume_skills(db, profile_id))
    if not pool:
        raise NoResumeAnalysisError("No placement-relevant resume skills found")
    return _assign_six(pool)


def _build_question_prompt(skill_tag: str) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return render_prompt(template, skill_tag=skill_tag)


def _challenge_question_read(question: Question, index: int) -> ChallengeQuestionRead:
    return ChallengeQuestionRead(
        id=question.id,
        index=index,
        skill_tag=question.skill_tag,
        prompt=question.prompt,
        options=question.options or [],
    )


def _completed_count(db: Session, challenge_id: int) -> int:
    return (
        db.query(PlacementAlarmQuestionAttempt)
        .filter(
            PlacementAlarmQuestionAttempt.challenge_id == challenge_id,
            PlacementAlarmQuestionAttempt.is_correct.is_(True),
        )
        .count()
    )


def _current_attempt(db: Session, challenge_id: int) -> Optional[PlacementAlarmQuestionAttempt]:
    return (
        db.query(PlacementAlarmQuestionAttempt)
        .filter(
            PlacementAlarmQuestionAttempt.challenge_id == challenge_id,
            PlacementAlarmQuestionAttempt.is_correct.is_(False),
        )
        .order_by(PlacementAlarmQuestionAttempt.question_index.asc())
        .first()
    )


def _state_read(db: Session, challenge: PlacementAlarmChallenge) -> ChallengeStateRead:
    current_question = None
    attempt = _current_attempt(db, challenge.id)
    if attempt is not None:
        question = db.get(Question, attempt.question_id)
        current_question = _challenge_question_read(question, attempt.question_index)

    return ChallengeStateRead(
        challenge_id=challenge.id,
        status=challenge.status,
        completed_count=_completed_count(db, challenge.id),
        current_question=current_question,
    )


def _get_owned_challenge(db: Session, challenge_id: int, profile_id: int) -> PlacementAlarmChallenge:
    challenge = db.get(PlacementAlarmChallenge, challenge_id)
    if challenge is None or challenge.profile_id != profile_id:
        raise ChallengeNotFoundError("Challenge not found")
    return challenge


def start_challenge(
    db: Session, profile_id: int, client_alarm_id: str, ai_provider: AIProvider
) -> ChallengeStateRead:
    """Idempotent per (profile, client_alarm_id): if an in-progress challenge already exists
    for this on-device alarm firing, its exact current state is returned instead of
    generating a new random set of questions -- this is what lets the challenge survive
    Back/app-close/process-death and still show the student the same 6 questions."""
    existing = (
        db.query(PlacementAlarmChallenge)
        .filter(
            PlacementAlarmChallenge.profile_id == profile_id,
            PlacementAlarmChallenge.client_alarm_id == client_alarm_id,
            PlacementAlarmChallenge.status == "in_progress",
        )
        .first()
    )
    if existing is not None:
        return _state_read(db, existing)

    skills = select_alarm_skills(db, profile_id)

    # Every question is generated first, outside any transaction and before a single row is
    # written (F12-07). This used to interleave: create the challenge, then per skill call the
    # AI, commit a Question, and leave that iteration's attempt row pending for the *next*
    # iteration's commit to persist -- so a transaction with unflushed work in it was held open
    # across all six model calls, six times the latency of any other AI route here.
    #
    # Generating first also removes the failure mode the old handler existed to clean up. A
    # mid-loop failure could previously leave a committed challenge with fewer than
    # REQUIRED_QUESTIONS attempts, which the idempotency check above would then hand back to
    # every retry forever; recovering from that meant deleting committed attempt rows before
    # the challenge, because challenge_id is a real foreign key and PostgreSQL enforces it.
    # Now nothing is written until every question exists, so there is no partial challenge to
    # repair and no ordering to get right.
    generated_questions: list[tuple[str, GeneratedQuestion]] = []
    with outside_transaction(db):
        try:
            for skill_tag in skills:
                prompt = _build_question_prompt(skill_tag)
                generated: GeneratedQuestion = ai_provider.generate_structured(prompt, GeneratedQuestion)
                generated_questions.append((skill_tag, _sanitize_generated_question(generated, skill_tag)))
        except AIGenerationError:
            raise ChallengeGenerationError("Couldn't generate challenge questions -- please try again")

    # Re-checked on this side of the boundary: another firing of the same on-device alarm could
    # have created the challenge while these six calls were running, and handing back the one
    # that already exists is what the idempotency contract promises.
    existing = (
        db.query(PlacementAlarmChallenge)
        .filter(
            PlacementAlarmChallenge.profile_id == profile_id,
            PlacementAlarmChallenge.client_alarm_id == client_alarm_id,
            PlacementAlarmChallenge.status == "in_progress",
        )
        .first()
    )
    if existing is not None:
        return _state_read(db, existing)

    # Then one short write, all of it or none of it.
    challenge = PlacementAlarmChallenge(profile_id=profile_id, client_alarm_id=client_alarm_id)
    db.add(challenge)
    db.flush()
    for index, (skill_tag, generated) in enumerate(generated_questions):
        question = Question(
            skill_tag=skill_tag,
            type="mcq",  # forced regardless of what the AI returned -- MCQ-only, by design
            difficulty="medium",
            prompt=generated.prompt,
            options=generated.options,
            expected_answer_or_rubric=generated.expected_answer_or_rubric,
            explanation=generated.explanation,
            source="ai_generated",
        )
        db.add(question)
        db.flush()
        db.add(
            PlacementAlarmQuestionAttempt(
                challenge_id=challenge.id, question_id=question.id, question_index=index
            )
        )
    db.commit()

    return _state_read(db, challenge)


def get_challenge_state(db: Session, profile_id: int, challenge_id: int) -> ChallengeStateRead:
    challenge = _get_owned_challenge(db, challenge_id, profile_id)
    return _state_read(db, challenge)


def submit_challenge_answer(
    db: Session, profile_id: int, challenge_id: int, question_id: int, student_answer: str
) -> ChallengeAnswerResult:
    challenge = _get_owned_challenge(db, challenge_id, profile_id)

    attempt = (
        db.query(PlacementAlarmQuestionAttempt)
        .filter(
            PlacementAlarmQuestionAttempt.challenge_id == challenge_id,
            PlacementAlarmQuestionAttempt.question_id == question_id,
        )
        .first()
    )
    if attempt is None:
        raise QuestionNotInChallengeError("No such question in this challenge")
    if attempt.is_correct:
        raise QuestionAlreadyCorrectError("This question was already answered correctly")

    question = db.get(Question, question_id)
    is_correct = student_answer.strip().lower() == question.expected_answer_or_rubric.strip().lower()

    attempt.attempt_count += 1
    attempt.last_student_answer = student_answer
    if is_correct:
        attempt.is_correct = True
    db.commit()

    completed_count = _completed_count(db, challenge_id)
    challenge_completed = completed_count == REQUIRED_QUESTIONS
    if challenge_completed and challenge.status != "completed":
        challenge.status = "completed"
        db.commit()

    return ChallengeAnswerResult(
        question_id=question.id,
        is_correct=is_correct,
        correct_answer=None if is_correct else question.expected_answer_or_rubric,
        explanation=None if is_correct else question.explanation,
        completed_count=completed_count,
        challenge_completed=challenge_completed,
    )
