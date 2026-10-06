import logging
from pathlib import Path
from typing import List, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
from app.ai.output_guard import contains_prompt_leak, leaking_field
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.assessment import AnswerGrade, GeneratedQuestion
from app.ai.score_utils import clamp_unit_score
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.question import Question
from app.models.question_attempt import QuestionAttempt
from app.schemas.assessment import (
    AnswerResult,
    AssessmentRead,
    AssessmentResultRead,
    BlueprintSection,
    NextQuestionResponse,
    QuestionRead,
    SkillScore,
)
from app.services.ai_boundary import outside_transaction

_QUESTION_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "question_generator_v2.txt"
_GRADER_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "answer_grader_v3.txt"

_DIFFICULTIES = ["easy", "medium", "hard"]
_WEAKNESS_THRESHOLD = 0.6
# The pass mark answer_grader_v3.txt documents for is_correct. Kept here, not in the prompt
# alone, because submit_answer now derives is_correct from the score rather than trusting the
# model's own boolean -- see the comment at that derivation.
_IS_CORRECT_SCORE_THRESHOLD = 0.6

logger = logging.getLogger(__name__)


def _sanitize_generated_question(generated: GeneratedQuestion, skill: str) -> GeneratedQuestion:
    """Code-level backstop, independent of the prompt's own hardening (see
    app/ai/output_guard.py's module docstring for why prompt wording alone was not reliable
    enough here): if skill_tag was ever attacker-influenced instruction-shaped text, this
    catches a model that echoed prompt scaffolding into `prompt`/`explanation`/
    `expected_answer_or_rubric` instead of generating a genuine question, and swaps in a safe
    generic question rather than ever surfacing the leak to a student.

    `expected_answer_or_rubric` is checked here too (a real audit gap: it previously wasn't)
    because it is exactly the field answer_grader_v3.txt re-interpolates as {rubric} for every
    non-mcq answer -- a leak that survived here would flow straight into a second AI prompt.

    The check is now over the whole generated object rather than three named fields. That
    closes a second gap of the same kind: `options` -- the four MCQ choices, persisted on
    Question.options and rendered to the student verbatim -- was never checked, so a model that
    leaked into an option instead of the prompt was displayed unaltered. Naming fields is how
    that gap happened; asking the schema is how it stops happening."""
    if contains_prompt_leak(generated):
        logger.warning(
            "Discarding a generated question for skill=%r: output contained prompt-leak markers.",
            skill,
        )
        if generated.type == "mcq":
            return GeneratedQuestion(
                type="mcq",
                prompt=f"Which of the following best relates to {skill!r}?",
                options=[
                    "A relevant concept",
                    "An unrelated concept",
                    "A different unrelated concept",
                    "None of these",
                ],
                expected_answer_or_rubric="A relevant concept",
                explanation="This question checks general understanding of the topic.",
            )
        return GeneratedQuestion(
            type="short_answer",
            prompt=f"Explain a key concept related to {skill!r} and why it matters in practice.",
            options=None,
            expected_answer_or_rubric="A correct, relevant explanation of the concept.",
            explanation="This question checks general understanding of the topic.",
        )
    return generated


class AssessmentNotFoundError(Exception):
    """Assessment/blueprint not found, or not owned by this profile."""


class QuestionNotPendingError(Exception):
    """No unanswered attempt exists for this assessment+question (already answered, or
    never issued)."""


class AssessmentNotCompleteError(Exception):
    """Result requested before every blueprint section has been answered."""


def _get_owned_assessment(db: Session, assessment_id: int, profile_id: int) -> Assessment:
    assessment = db.get(Assessment, assessment_id)
    if assessment is None or assessment.profile_id != profile_id:
        raise AssessmentNotFoundError("Assessment not found")
    return assessment


def read_assessment(db: Session, profile_id: int, assessment_id: int) -> Optional[AssessmentRead]:
    """This student's assessment as start_assessment would have returned it, or None.

    Used to answer a replayed start request from the row the first attempt created, rather than
    from a cached response -- so the status is whatever it is *now*, and an assessment the
    student has since deleted reports as gone instead of being invented. Ownership is checked
    here and not assumed from the claim, so a result id can never be read across profiles.
    """
    assessment = db.get(Assessment, assessment_id)
    if assessment is None or assessment.profile_id != profile_id:
        return None
    return AssessmentRead(
        id=assessment.id, blueprint_id=assessment.blueprint_id, status=assessment.status
    )


def start_assessment(db: Session, profile_id: int, blueprint_id: int) -> AssessmentRead:
    blueprint = db.get(AssessmentBlueprint, blueprint_id)
    if blueprint is None or blueprint.profile_id != profile_id:
        raise AssessmentNotFoundError("Blueprint not found")

    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint_id, status="in_progress")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return AssessmentRead(id=assessment.id, blueprint_id=assessment.blueprint_id, status=assessment.status)


def _sections(blueprint: AssessmentBlueprint) -> List[BlueprintSection]:
    return [BlueprintSection(**s) for s in blueprint.sections]


def _answered_attempts(db: Session, assessment_id: int) -> List[QuestionAttempt]:
    return (
        db.query(QuestionAttempt)
        .filter(QuestionAttempt.assessment_id == assessment_id, QuestionAttempt.student_answer.isnot(None))
        .all()
    )


def _pending_attempt(db: Session, assessment_id: int) -> Optional[QuestionAttempt]:
    return (
        db.query(QuestionAttempt)
        .filter(QuestionAttempt.assessment_id == assessment_id, QuestionAttempt.student_answer.is_(None))
        .first()
    )


def _skill_tags_by_question_id(db: Session, question_ids: List[int]) -> dict:
    """Single bulk lookup instead of one `db.get(Question, ...)` per attempt -- same
    pattern interview_service already uses for the equivalent problem."""
    if not question_ids:
        return {}
    return {q.id: q.skill_tag for q in db.query(Question).filter(Question.id.in_(question_ids)).all()}


def _question_read(question: Question) -> QuestionRead:
    return QuestionRead(
        id=question.id,
        skill_tag=question.skill_tag,
        type=question.type,
        difficulty=question.difficulty,
        prompt=question.prompt,
        options=question.options,
    )


def _next_difficulty(db: Session, assessment_id: int, skill_tag: str) -> str:
    """Adaptive step: escalate one level on a correct answer, de-escalate on incorrect,
    starting at medium for a skill's first question."""
    last = (
        db.query(QuestionAttempt, Question.difficulty, QuestionAttempt.is_correct)
        .join(Question, Question.id == QuestionAttempt.question_id)
        .filter(
            QuestionAttempt.assessment_id == assessment_id,
            Question.skill_tag == skill_tag,
            QuestionAttempt.student_answer.isnot(None),
        )
        .order_by(QuestionAttempt.id.desc())
        .first()
    )
    if last is None:
        return "medium"

    _, difficulty, is_correct = last
    idx = _DIFFICULTIES.index(difficulty)
    idx = min(idx + 1, len(_DIFFICULTIES) - 1) if is_correct else max(idx - 1, 0)
    return _DIFFICULTIES[idx]


def _build_question_prompt(skill_tag: str, assessment_type: str, difficulty: str) -> str:
    template = _QUESTION_PROMPT_PATH.read_text(encoding="utf-8")
    return render_prompt(template, skill_tag=skill_tag, assessment_type=assessment_type, difficulty=difficulty)


def get_next_question(
    db: Session, profile_id: int, assessment_id: int, ai_provider: AIProvider
) -> NextQuestionResponse:
    assessment = _get_owned_assessment(db, assessment_id, profile_id)
    if assessment.status != "in_progress":
        return NextQuestionResponse(completed=True, question=None)

    pending = _pending_attempt(db, assessment_id)
    if pending is not None:
        question = db.get(Question, pending.question_id)
        return NextQuestionResponse(completed=False, question=_question_read(question))

    blueprint = db.get(AssessmentBlueprint, assessment.blueprint_id)
    sections = _sections(blueprint)

    answered = _answered_attempts(db, assessment_id)
    skill_tags = _skill_tags_by_question_id(db, [a.question_id for a in answered])
    answered_counts: dict = {}
    for attempt in answered:
        skill_tag = skill_tags[attempt.question_id]
        answered_counts[skill_tag] = answered_counts.get(skill_tag, 0) + 1

    current_section = next(
        (s for s in sections if answered_counts.get(s.skill, 0) < s.question_count), None
    )

    if current_section is None:
        assessment.status = "completed"
        db.commit()
        return NextQuestionResponse(completed=True, question=None)

    difficulty = _next_difficulty(db, assessment_id, current_section.skill)
    # Plain values, taken before the boundary below closes the session.
    skill = current_section.skill
    assessment_type = current_section.assessment_type
    prompt = _build_question_prompt(skill, assessment_type, difficulty)
    # Outside any transaction (F12-07). Question generation is a full model call, and holding a
    # pooled connection and a PostgreSQL snapshot for it bought nothing -- everything it needs is
    # already in `prompt`.
    with outside_transaction(db):
        generated: GeneratedQuestion = ai_provider.generate_structured(prompt, GeneratedQuestion)
        generated = _sanitize_generated_question(generated, skill)

    question = Question(
        skill_tag=skill,
        type=generated.type,
        difficulty=difficulty,
        prompt=generated.prompt,
        options=generated.options,
        expected_answer_or_rubric=generated.expected_answer_or_rubric,
        explanation=generated.explanation,
        source="ai_generated",
    )
    db.add(question)
    # flush, not commit: the question and the attempt that makes it the student's pending question
    # are one unit. Committing the question on its own (which is what this did) could leave a
    # generated question with no attempt pointing at it.
    db.flush()
    db.add(QuestionAttempt(assessment_id=assessment_id, question_id=question.id))
    db.commit()

    return NextQuestionResponse(completed=False, question=_question_read(question))


def _stored_answer_result(
    db: Session, assessment_id: int, question_id: int, student_answer: str
) -> Optional[AnswerResult]:
    """This question's already-stored result, but only when the request is a resend of it.

    Phase 31. The whole response is reconstructible from durable state -- `is_correct` and `score`
    are columns on the attempt and `explanation` belongs to the question -- so a resend can be
    answered without regrading anything and without a second row.

    `student_answer` is what decides identity, and it is exact: OkHttp resends the same body byte
    for byte, so a resend always matches, while an attempt to answer differently never does and
    keeps getting the conflict it got before. `time_taken_seconds` is deliberately not compared --
    it is metadata about the attempt, not what the mutation stored a judgement of, and a request
    that differs only there is still the same answer to the same question.

    Scoped to this assessment and question, and only ever reached after the caller has checked
    that this assessment belongs to this profile, so a matching id alone can never surface
    somebody else's answer.
    """
    answered = (
        db.query(QuestionAttempt)
        .populate_existing()
        .filter(
            QuestionAttempt.assessment_id == assessment_id,
            QuestionAttempt.question_id == question_id,
            QuestionAttempt.student_answer.isnot(None),
        )
        .first()
    )
    if answered is None or answered.student_answer != student_answer:
        return None
    question = db.get(Question, question_id)
    if question is None:
        return None
    return AnswerResult(
        question_id=question_id,
        is_correct=bool(answered.is_correct),
        score=answered.score,
        explanation=question.explanation,
    )


def submit_answer(
    db: Session,
    profile_id: int,
    assessment_id: int,
    question_id: int,
    student_answer: str,
    time_taken_seconds: Optional[int],
    ai_provider: AIProvider,
) -> AnswerResult:
    _get_owned_assessment(db, assessment_id, profile_id)

    attempt = (
        db.query(QuestionAttempt)
        .filter(
            QuestionAttempt.assessment_id == assessment_id,
            QuestionAttempt.question_id == question_id,
            QuestionAttempt.student_answer.is_(None),
        )
        .first()
    )
    if attempt is None:
        # Phase 31: nothing is pending, so either this answer was already stored and the response
        # was lost, or the student is trying to answer a question they have already answered. The
        # first is owed its result; the second is still refused. Checked here, before any grading,
        # so a resend costs no model work.
        replayed = _stored_answer_result(db, assessment_id, question_id, student_answer)
        if replayed is not None:
            return replayed
        raise QuestionNotPendingError("No pending question with this id for this assessment")

    question = db.get(Question, question_id)
    # Plain values, taken now: the non-mcq branch below closes the session for the grading call,
    # and both the conditional UPDATE and the response still need these afterwards.
    attempt_id = attempt.id
    question_explanation = question.explanation

    if question.type == "mcq":
        is_correct = student_answer.strip().lower() == question.expected_answer_or_rubric.strip().lower()
        score = 1.0 if is_correct else 0.0
    elif not student_answer.strip():
        # Deterministic, before any AI call: a blank (or whitespace-only) answer is 0.0 by
        # definition, and no model output can override that. answer_grader_v3.txt already
        # *instructs* the grader to score a blank answer 0.0, but that is a prompt-level
        # control over an untrusted-by-design output -- the historical exploit recorded in
        # tests/unit/test_prompt_injection_hardening.py was, verbatim, "a blank/irrelevant
        # assessment answer scored 1.0/is_correct=True". Short-circuiting here makes the
        # security requirement ("a blank answer must never become correct") independent of
        # model behaviour entirely, and skips a pointless paid AI call as a side effect.
        is_correct = False
        score = 0.0
    else:
        grade_prompt = render_prompt(
            _GRADER_PROMPT_PATH.read_text(encoding="utf-8"),
            question=question.prompt,
            rubric=question.expected_answer_or_rubric,
            student_answer=student_answer,
        )
        # Outside any transaction (F12-07). Safe to leave here without re-reading anything,
        # because the write below is already a conditional UPDATE rather than a load-mutate-commit:
        # whatever changed while the model was grading, only the request that flips a still-NULL
        # student_answer row wins. That was written for the same race this boundary widens, so
        # widening it changes nothing about who wins.
        with outside_transaction(db):
            grade: AnswerGrade = ai_provider.generate_structured(grade_prompt, AnswerGrade)
        # Found by tests/unit/test_ai_output_guard_parity.py, not by the audit: this call site
        # consumes only `score` today, so no generated text currently crosses the boundary here
        # (AnswerGrade.feedback is never read, and is_correct is derived from the clamped score
        # rather than taken from the model). That is a property of the current code, not of the
        # contract -- surfacing the model's feedback to the student is an obvious future change,
        # and it would arrive already leaking.
        #
        # The guard is over the response as a whole rather than over the field that happens to
        # be read, because a grade that echoes prompt scaffolding is not a judgement of this
        # answer: its number is no more trustworthy than its prose. Routed as a generation
        # failure, which is the path this service already has for a grade it could not obtain --
        # raised before the conditional UPDATE below, so the attempt stays pending and the
        # student can resubmit rather than being scored on a hijacked response.
        leaked = leaking_field(grade)
        if leaked is not None:
            raise AIGenerationError(f"Answer grade output leaked prompt scaffolding in field {leaked}")
        score = clamp_unit_score(
            grade.score, field_name="score", context=f"assessment answer grading, question_id={question_id}"
        )
        # is_correct is DERIVED from the clamped score, never taken from the model's own
        # boolean. AnswerGrade.is_correct and AnswerGrade.score are two independently
        # forgeable fields on the same untrusted response: answer_grader_v3.txt documents the
        # relationship ("is_correct is true only if score >= 0.6"), but nothing enforced it,
        # so a response of {"score": 0.0, "is_correct": true} -- exactly what a partially
        # successful "give full marks" injection produces -- was persisted and returned as a
        # correct answer despite a 0.0 score. Deriving it removes the second forgeable field
        # while leaving genuine partial credit (the score itself) entirely intact.
        is_correct = score >= _IS_CORRECT_SCORE_THRESHOLD

    # Atomic conditional UPDATE (not "load, mutate, commit") -- same shape as
    # mission_service.complete / subscription_service._activate_paid for the identical class
    # of race: two concurrent submissions for the same pending question can both pass the
    # "still pending" read above (and, for a non-mcq question, both complete a real AI grading
    # call) before either commits. Only the request whose UPDATE actually flips a still-NULL
    # student_answer row wins; the loser's grade -- however it was computed -- is discarded
    # here rather than silently overwriting the winner's already-persisted grade.
    result = db.execute(
        update(QuestionAttempt)
        .where(QuestionAttempt.id == attempt_id, QuestionAttempt.student_answer.is_(None))
        .values(
            student_answer=student_answer,
            is_correct=is_correct,
            score=score,
            time_taken_seconds=time_taken_seconds,
        )
    )
    db.commit()
    if not result.rowcount:
        # Lost the atomic UPDATE race. The winner's grade is this question's grade, and the
        # loser's -- however it was computed -- is discarded, exactly as before. What changed in
        # Phase 31 is the answer given back when the two requests asked the same thing: the
        # winner's stored result rather than a 409.
        replayed = _stored_answer_result(db, assessment_id, question_id, student_answer)
        if replayed is not None:
            return replayed
        raise QuestionNotPendingError("No pending question with this id for this assessment")

    return AnswerResult(
        question_id=question_id, is_correct=is_correct, score=score, explanation=question_explanation
    )


def get_result(db: Session, profile_id: int, assessment_id: int) -> AssessmentResultRead:
    _get_owned_assessment(db, assessment_id, profile_id)
    assessment = db.get(Assessment, assessment_id)
    if assessment.status != "completed":
        raise AssessmentNotCompleteError("Assessment is not complete yet")

    existing = db.query(AssessmentResult).filter(AssessmentResult.assessment_id == assessment_id).first()
    if existing is not None:
        return AssessmentResultRead(
            assessment_id=assessment_id,
            overall_score=existing.overall_score,
            skill_scores=[SkillScore(skill_tag=k, score=v) for k, v in existing.skill_scores.items()],
            priority_weaknesses=existing.priority_weaknesses,
        )

    answered = _answered_attempts(db, assessment_id)
    skill_tags = _skill_tags_by_question_id(db, [a.question_id for a in answered])
    scores_by_skill: dict = {}
    for attempt in answered:
        scores_by_skill.setdefault(skill_tags[attempt.question_id], []).append(attempt.score)

    skill_scores = {skill: sum(vals) / len(vals) for skill, vals in scores_by_skill.items()}
    overall_score = sum(skill_scores.values()) / len(skill_scores) if skill_scores else 0.0
    priority_weaknesses = sorted(
        (skill for skill, score in skill_scores.items() if score < _WEAKNESS_THRESHOLD),
        key=lambda skill: skill_scores[skill],
    )

    result = AssessmentResult(
        assessment_id=assessment_id,
        skill_scores=skill_scores,
        overall_score=overall_score,
        priority_weaknesses=priority_weaknesses,
    )
    db.add(result)
    db.commit()

    return AssessmentResultRead(
        assessment_id=assessment_id,
        overall_score=overall_score,
        skill_scores=[SkillScore(skill_tag=k, score=v) for k, v in skill_scores.items()],
        priority_weaknesses=priority_weaknesses,
    )
