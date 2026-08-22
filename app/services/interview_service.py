from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.ai.provider import AIProvider
from app.ai.schemas.interview import GeneratedInterviewEvaluation, GeneratedInterviewQuestion
from app.models.interview import Interview
from app.models.interview_answer import InterviewAnswer
from app.models.interview_evaluation import InterviewEvaluation
from app.models.interview_question import InterviewQuestion
from app.models.job_description import JobDescription
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.role import Role
from app.schemas.interview import (
    InterviewAnswerRead,
    InterviewEvaluationRead,
    InterviewQuestionRead,
    InterviewRead,
    NextInterviewQuestionResponse,
)
from app.services.skill_matching import NoResumeAnalysisError, latest_resume_skills

_QUESTION_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "interview_question_v1.txt"
_EVALUATION_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "interview_evaluation_v1.txt"

_QUESTIONS_PER_INTERVIEW = 5


class InterviewNotFoundError(Exception):
    """Interview (or a referenced question) not found, or not owned by this profile."""


class InvalidInterviewRequestError(Exception):
    """Bad start-interview request: unsupported mode, or a jd_id that doesn't resolve."""


class InterviewQuestionNotPendingError(Exception):
    """This interview question has already been answered."""


class InterviewNotCompleteError(Exception):
    """Evaluation requested before all questions have been answered."""


def _get_owned_interview(db: Session, interview_id: int, profile_id: int) -> Interview:
    interview = db.get(Interview, interview_id)
    if interview is None or interview.profile_id != profile_id:
        raise InterviewNotFoundError("Interview not found")
    return interview


def _interview_read(interview: Interview) -> InterviewRead:
    return InterviewRead(
        id=interview.id, type=interview.type, mode=interview.mode, jd_id=interview.jd_id, status=interview.status
    )


def _question_read(question: InterviewQuestion) -> InterviewQuestionRead:
    return InterviewQuestionRead(
        id=question.id, order_index=question.order_index, question_text=question.question_text,
        based_on=question.based_on,
    )


def start_interview(
    db: Session, profile_id: int, interview_type: str, mode: str, jd_id: Optional[int]
) -> InterviewRead:
    if mode != "text":
        raise InvalidInterviewRequestError("Voice interviews aren't available yet (Stage 10)")

    if jd_id is not None:
        jd = db.get(JobDescription, jd_id)
        if jd is None or jd.profile_id != profile_id:
            raise InvalidInterviewRequestError("Job description not found")

    interview = Interview(profile_id=profile_id, type=interview_type, mode=mode, jd_id=jd_id, status="in_progress")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    return _interview_read(interview)


def _role_name(db: Session, role_id: Optional[int]) -> Optional[str]:
    if role_id is None:
        return None
    role = db.get(Role, role_id)
    return role.name if role else None


def _build_context(db: Session, profile_id: int, interview: Interview) -> dict:
    role_name = None
    jd_text = None

    if interview.jd_id is not None:
        jd = db.get(JobDescription, interview.jd_id)
        if jd is not None:
            jd_text = jd.raw_text
            role_name = _role_name(db, jd.detected_role_id)

    if role_name is None:
        profile = db.get(StudentProfile, profile_id)
        if profile is not None:
            role_name = _role_name(db, profile.target_role_id)

    try:
        resume_skills = latest_resume_skills(db, profile_id)
    except NoResumeAnalysisError:
        resume_skills = []

    projects = db.query(Project).filter(Project.profile_id == profile_id).all()

    return {
        "role_name": role_name or "the target role",
        "jd_text": jd_text or "No job description provided.",
        "resume_skills": resume_skills,
        "projects": projects,
    }


def _build_question_prompt(
    interview_type: str, context: dict, asked_questions: List[str], question_number: int, total_questions: int
) -> str:
    template = _QUESTION_PROMPT_PATH.read_text(encoding="utf-8")
    projects_text = "\n".join(
        f"- {p.title}: {p.description or ''} (tech: {p.tech_stack or 'unspecified'})" for p in context["projects"]
    ) or "No projects listed."
    skills_text = ", ".join(context["resume_skills"]) or "No resume on file."
    asked_text = "\n".join(f"- {q}" for q in asked_questions) or "None yet -- this is the first question."

    return template.format(
        interview_type=interview_type,
        role_name=context["role_name"],
        jd_text=context["jd_text"],
        resume_skills=skills_text,
        projects=projects_text,
        previously_asked=asked_text,
        question_number=question_number,
        total_questions=total_questions,
    )


def get_next_question(
    db: Session, profile_id: int, interview_id: int, ai_provider: AIProvider
) -> NextInterviewQuestionResponse:
    interview = _get_owned_interview(db, interview_id, profile_id)
    if interview.status != "in_progress":
        return NextInterviewQuestionResponse(completed=True, question=None)

    questions = (
        db.query(InterviewQuestion)
        .filter(InterviewQuestion.interview_id == interview_id)
        .order_by(InterviewQuestion.order_index)
        .all()
    )
    question_ids = [q.id for q in questions]
    answered_ids = (
        {
            a.interview_question_id
            for a in db.query(InterviewAnswer)
            .filter(InterviewAnswer.interview_question_id.in_(question_ids))
            .all()
        }
        if question_ids
        else set()
    )

    pending = next((q for q in questions if q.id not in answered_ids), None)
    if pending is not None:
        return NextInterviewQuestionResponse(completed=False, question=_question_read(pending))

    if len(questions) >= _QUESTIONS_PER_INTERVIEW:
        interview.status = "completed"
        db.commit()
        return NextInterviewQuestionResponse(completed=True, question=None)

    context = _build_context(db, profile_id, interview)
    prompt = _build_question_prompt(
        interview.type, context, [q.question_text for q in questions], len(questions) + 1, _QUESTIONS_PER_INTERVIEW
    )
    generated: GeneratedInterviewQuestion = ai_provider.generate_structured(prompt, GeneratedInterviewQuestion)

    question = InterviewQuestion(
        interview_id=interview_id,
        question_text=generated.question_text,
        order_index=len(questions) + 1,
        based_on=generated.based_on,
    )
    db.add(question)
    db.commit()
    db.refresh(question)

    return NextInterviewQuestionResponse(completed=False, question=_question_read(question))


def submit_answer(
    db: Session, profile_id: int, interview_id: int, interview_question_id: int, answer_text: str
) -> InterviewAnswerRead:
    _get_owned_interview(db, interview_id, profile_id)

    question = db.get(InterviewQuestion, interview_question_id)
    if question is None or question.interview_id != interview_id:
        raise InterviewNotFoundError("Interview question not found")

    existing = (
        db.query(InterviewAnswer)
        .filter(InterviewAnswer.interview_question_id == interview_question_id)
        .first()
    )
    if existing is not None:
        raise InterviewQuestionNotPendingError("This question has already been answered")

    answer = InterviewAnswer(
        interview_question_id=interview_question_id, answer_text=answer_text, transcribed=False
    )
    db.add(answer)
    db.commit()

    return InterviewAnswerRead(interview_question_id=interview_question_id, answer_text=answer_text)


def _build_evaluation_prompt(interview_type: str, context: dict, transcript: str) -> str:
    template = _EVALUATION_PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(
        interview_type=interview_type, role_name=context["role_name"], jd_text=context["jd_text"],
        transcript=transcript,
    )


def _evaluation_read(evaluation: InterviewEvaluation) -> InterviewEvaluationRead:
    return InterviewEvaluationRead(
        technical_correctness=evaluation.technical_correctness,
        relevance=evaluation.relevance,
        completeness=evaluation.completeness,
        reasoning=evaluation.reasoning,
        structure=evaluation.structure,
        clarity=evaluation.clarity,
        grammar=evaluation.grammar,
        speaking_pace=evaluation.speaking_pace,
        filler_word_rate=evaluation.filler_word_rate,
        role_jd_coverage=evaluation.role_jd_coverage,
        overall_score=evaluation.overall_score,
        feedback=evaluation.feedback_text,
    )


def get_evaluation(
    db: Session, profile_id: int, interview_id: int, ai_provider: AIProvider
) -> InterviewEvaluationRead:
    interview = _get_owned_interview(db, interview_id, profile_id)
    if interview.status != "completed":
        raise InterviewNotCompleteError("Interview is not complete yet")

    existing = db.query(InterviewEvaluation).filter(InterviewEvaluation.interview_id == interview_id).first()
    if existing is not None:
        return _evaluation_read(existing)

    questions = (
        db.query(InterviewQuestion)
        .filter(InterviewQuestion.interview_id == interview_id)
        .order_by(InterviewQuestion.order_index)
        .all()
    )
    question_ids = [q.id for q in questions]
    answers_by_question_id = (
        {
            a.interview_question_id: a
            for a in db.query(InterviewAnswer)
            .filter(InterviewAnswer.interview_question_id.in_(question_ids))
            .all()
        }
        if question_ids
        else {}
    )

    transcript = "\n\n".join(
        f"Q{q.order_index}: {q.question_text}\n"
        f"A{q.order_index}: {answers_by_question_id[q.id].answer_text if q.id in answers_by_question_id else '(no answer given)'}"
        for q in questions
    )

    context = _build_context(db, profile_id, interview)
    prompt = _build_evaluation_prompt(interview.type, context, transcript)
    generated: GeneratedInterviewEvaluation = ai_provider.generate_structured(prompt, GeneratedInterviewEvaluation)

    evaluation = InterviewEvaluation(
        interview_id=interview_id,
        technical_correctness=generated.technical_correctness,
        relevance=generated.relevance,
        completeness=generated.completeness,
        reasoning=generated.reasoning,
        structure=generated.structure,
        clarity=generated.clarity,
        grammar=generated.grammar,
        speaking_pace=None,
        filler_word_rate=None,
        role_jd_coverage=generated.role_jd_coverage,
        overall_score=generated.overall_score,
        feedback_text=generated.feedback,
    )
    db.add(evaluation)
    db.commit()

    return _evaluation_read(evaluation)
