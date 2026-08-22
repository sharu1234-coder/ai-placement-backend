from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.ai.provider import AIProvider
from app.ai.schemas.assessment import AnswerGrade, GeneratedQuestion
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

_QUESTION_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "question_generator_v1.txt"
_GRADER_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "answer_grader_v1.txt"

_DIFFICULTIES = ["easy", "medium", "hard"]
_WEAKNESS_THRESHOLD = 0.6


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
    return template.format(skill_tag=skill_tag, assessment_type=assessment_type, difficulty=difficulty)


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
    prompt = _build_question_prompt(current_section.skill, current_section.assessment_type, difficulty)
    generated: GeneratedQuestion = ai_provider.generate_structured(prompt, GeneratedQuestion)

    question = Question(
        skill_tag=current_section.skill,
        type=generated.type,
        difficulty=difficulty,
        prompt=generated.prompt,
        options=generated.options,
        expected_answer_or_rubric=generated.expected_answer_or_rubric,
        explanation=generated.explanation,
        source="ai_generated",
    )
    db.add(question)
    db.commit()
    db.refresh(question)

    db.add(QuestionAttempt(assessment_id=assessment_id, question_id=question.id))
    db.commit()

    return NextQuestionResponse(completed=False, question=_question_read(question))


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
        raise QuestionNotPendingError("No pending question with this id for this assessment")

    question = db.get(Question, question_id)

    if question.type == "mcq":
        is_correct = student_answer.strip().lower() == question.expected_answer_or_rubric.strip().lower()
        score = 1.0 if is_correct else 0.0
    else:
        grade_prompt = _GRADER_PROMPT_PATH.read_text(encoding="utf-8").format(
            question=question.prompt,
            rubric=question.expected_answer_or_rubric,
            student_answer=student_answer,
        )
        grade: AnswerGrade = ai_provider.generate_structured(grade_prompt, AnswerGrade)
        is_correct = grade.is_correct
        score = grade.score

    attempt.student_answer = student_answer
    attempt.is_correct = is_correct
    attempt.score = score
    attempt.time_taken_seconds = time_taken_seconds
    db.commit()

    return AnswerResult(question_id=question.id, is_correct=is_correct, score=score, explanation=question.explanation)


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
