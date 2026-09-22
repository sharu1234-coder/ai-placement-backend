import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
from app.ai.output_guard import looks_like_prompt_leak
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.interview import GeneratedInterviewEvaluation, GeneratedInterviewQuestion
from app.ai.score_utils import clamp_unit_score
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
    InterviewHistoryItem,
    InterviewQuestionRead,
    InterviewRead,
    NextInterviewQuestionResponse,
)
from app.services import weakness_record_service
from app.services.interview_simulation import context as simulation_context
from app.services.interview_simulation import fallback as simulation_fallback
from app.services.interview_simulation import stages
from app.services.skill_matching import NoResumeAnalysisError, latest_resume_skills
from app.services.weakness_service import INTERVIEW_DIMENSIONS

logger = logging.getLogger(__name__)

_QUESTION_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "interview_question_v4.txt"
_EVALUATION_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "interview_evaluation_v3.txt"

_SIMULATION_QUESTION_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "ai" / "prompts" / "full_simulation_question_v1.txt"
)

_QUESTIONS_PER_INTERVIEW = 5


def _total_questions(interview_type: str) -> int:
    """How many questions this kind of interview asks. The only place the two lengths meet.

    A Full Recruiter Simulation is longer than a quick practice interview because it walks a
    whole conversation rather than probing one area, and its length is owned by the stage plan
    (which derives it from the plan itself, so the count and the plan cannot disagree). Every
    pre-existing type keeps the five questions it has always had.
    """
    return stages.SIMULATION_QUESTION_COUNT if stages.is_simulation(interview_type) else _QUESTIONS_PER_INTERVIEW

# Safety cap, not a product pagination feature (disclosed gap -- see assessment_service.py's
# _MAX_HISTORY_ROWS for the full rationale): protects response size for a long-lived account.
_MAX_HISTORY_ROWS = 200

# Deterministic, non-AI (matches the codebase's "our own gap engine" philosophy). Computed
# from the transcript text alone -- speaking_pace would need audio duration, which isn't
# available since raw audio is never persisted (see "minimize stored voice data").
_FILLER_PHRASES = ["um", "uh", "umm", "uhh", "erm", "like", "you know", "sort of", "kind of", "basically"]


def _filler_word_rate(text: str) -> float:
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if not words:
        return 0.0
    lowered = " ".join(words)
    filler_count = sum(lowered.count(phrase) for phrase in _FILLER_PHRASES)
    return round(min(filler_count / len(words), 1.0), 3)


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


def _insert_answer_once(
    db: Session, interview_question_id: int, answer_text: str, *, transcribed: bool
) -> None:
    """Persist the one answer this question is allowed, or raise if it already has one.

    The uniqueness of an answer is enforced by the database, not by the read above the call
    site: InterviewAnswer.interview_question_id carries a unique index, and that index -- not
    a preceding SELECT -- is what actually decides the winner when two submissions for the
    same question arrive at once. The "load, check, insert" shape both callers used lets two
    concurrent requests pass the same "no answer exists" check before either commits, so the
    loser's INSERT hit the index and surfaced as an uncaught IntegrityError, i.e. a 500 on a
    request the student had every reason to think was ordinary.

    Same class of race, and the same resolution, as recruiter_attack_service.answer_turn --
    the primitive differs only because an attack turn already exists with a NULL answer (so an
    atomic conditional UPDATE is the guard there) whereas an interview answer is a row that
    does not exist yet (so the unique index is the guard here). Both end in exactly one stored
    answer and a deterministic 409 for the loser, which is what the sequential duplicate path
    has always returned.

    Deliberately narrow: this treats an IntegrityError as "someone else answered first",
    because with the question row already loaded and validated by the caller, the unique index
    is the only constraint on this INSERT that can realistically fire. It never converts the
    failure into a success -- the loser is told its answer was not taken, rather than being
    left believing it was stored.
    """
    db.add(
        InterviewAnswer(
            interview_question_id=interview_question_id, answer_text=answer_text, transcribed=transcribed
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise InterviewQuestionNotPendingError("This question has already been answered") from exc


def start_interview(
    db: Session, profile_id: int, interview_type: str, mode: str, jd_id: Optional[int]
) -> InterviewRead:
    if jd_id is not None:
        jd = db.get(JobDescription, jd_id)
        if jd is None or jd.profile_id != profile_id:
            raise InvalidInterviewRequestError("Job description not found")

    interview = Interview(profile_id=profile_id, type=interview_type, mode=mode, jd_id=jd_id, status="in_progress")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    return _interview_read(interview)


def list_interviews(db: Session, profile_id: int) -> List[InterviewHistoryItem]:
    """Past interviews, most recent first -- there was previously no way to revisit one
    after finishing its live flow. overall_score stays None until get_evaluation has been
    called at least once for that interview (it computes InterviewEvaluation lazily)."""
    interviews = (
        db.query(Interview)
        .filter(Interview.profile_id == profile_id)
        .order_by(Interview.created_at.desc())
        .limit(_MAX_HISTORY_ROWS)
        .all()
    )
    interview_ids = [i.id for i in interviews]
    evaluations_by_interview: Dict[int, InterviewEvaluation] = {}
    if interview_ids:
        evaluations = (
            db.query(InterviewEvaluation).filter(InterviewEvaluation.interview_id.in_(interview_ids)).all()
        )
        evaluations_by_interview = {e.interview_id: e for e in evaluations}

    items: List[InterviewHistoryItem] = []
    for interview in interviews:
        evaluation = evaluations_by_interview.get(interview.id)
        items.append(
            InterviewHistoryItem(
                id=interview.id,
                type=interview.type,
                mode=interview.mode,
                status=interview.status,
                overall_score=evaluation.overall_score if evaluation else None,
                created_at=interview.created_at,
            )
        )
    return items


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

    return render_prompt(
        template,
        interview_type=interview_type,
        role_name=context["role_name"],
        jd_text=context["jd_text"],
        resume_skills=skills_text,
        projects=projects_text,
        previously_asked=asked_text,
        question_number=question_number,
        total_questions=total_questions,
    )


def _build_simulation_prompt(
    db: Session,
    profile: StudentProfile,
    interview: Interview,
    prior_pairs: List[tuple],
    order_index: int,
) -> str:
    """The stage-aware recruiter prompt. Everything variable here is decided by code first.

    The model is told which stage it is in, what that stage is for, and what the candidate has
    genuinely claimed -- and is then asked for one question's wording. It is never asked which
    stage this is, how many remain, or whether to move on. Every untrusted value goes through
    render_prompt, so a forged delimiter inside a resume line or a previous answer cannot break
    out of its block.
    """
    stage = stages.stage_for(order_index)
    context = simulation_context.build(db, profile, interview)

    return render_prompt(
        _SIMULATION_QUESTION_PROMPT_PATH.read_text(encoding="utf-8"),
        question_number=order_index,
        total_questions=stages.SIMULATION_QUESTION_COUNT,
        stage_label=stages.stage_label(stage),
        stage_intent=stages.stage_intent(stage),
        role_name=context.role_name,
        jd_block=simulation_context.render_jd_block(context),
        claims_block=simulation_context.render_claims_block(context),
        projects_block=simulation_context.render_projects_block(context),
        focus_block=simulation_context.render_focus_block(context),
        recent_turns=simulation_context.render_recent_turns(prior_pairs),
    )


def _next_question_response(
    interview: Interview, question: Optional[InterviewQuestion], *, completed: bool
) -> NextInterviewQuestionResponse:
    """Wraps a question (or the end of the interview) with the orchestration metadata the
    client needs but must never compute.

    `total_questions` is sent for every interview type, not just simulations: the Android screen
    previously hard-coded 5 to render "Question 2 of 5", which is a second copy of a backend
    constant sitting in another repository. Sending it removes that copy. `stage` stays null for
    the existing types, which genuinely have no stages -- an older client ignores both fields
    and behaves exactly as before.
    """
    total = _total_questions(interview.type)
    stage: Optional[str] = None
    if stages.is_simulation(interview.type):
        stage = stages.stage_for(question.order_index) if question is not None else stages.STAGE_CLOSING

    return NextInterviewQuestionResponse(
        completed=completed,
        question=_question_read(question) if question is not None else None,
        stage=stage,
        stage_label=stages.stage_label(stage) if stage is not None else None,
        question_number=question.order_index if question is not None else None,
        total_questions=total,
    )


def get_next_question(
    db: Session, profile_id: int, interview_id: int, ai_provider: AIProvider
) -> NextInterviewQuestionResponse:
    interview = _get_owned_interview(db, interview_id, profile_id)
    if interview.status != "in_progress":
        return _next_question_response(interview, None, completed=True)

    questions = (
        db.query(InterviewQuestion)
        .filter(InterviewQuestion.interview_id == interview_id)
        .order_by(InterviewQuestion.order_index)
        .all()
    )
    question_ids = [q.id for q in questions]
    # The answer TEXT, not just which ids are answered: a simulation question is written with
    # the last few exchanges in view, so the recruiter can follow up on what was actually said.
    # Same single query as before -- this reads the rows it was already fetching.
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

    pending = next((q for q in questions if q.id not in answers_by_question_id), None)
    if pending is not None:
        return _next_question_response(interview, pending, completed=False)

    total_questions = _total_questions(interview.type)
    if len(questions) >= total_questions:
        interview.status = "completed"
        db.commit()
        return _next_question_response(interview, None, completed=True)

    order_index = len(questions) + 1
    if stages.is_simulation(interview.type):
        profile = db.get(StudentProfile, profile_id)
        prior_pairs = [
            (q.question_text, answers_by_question_id[q.id].answer_text if q.id in answers_by_question_id else None)
            for q in questions
        ]
        prompt = _build_simulation_prompt(db, profile, interview, prior_pairs, order_index)
    else:
        context = _build_context(db, profile_id, interview)
        prompt = _build_question_prompt(
            interview.type, context, [q.question_text for q in questions], order_index, total_questions
        )
    try:
        generated: GeneratedInterviewQuestion = ai_provider.generate_structured(
            prompt, GeneratedInterviewQuestion
        )
        # Output-side guard, the same one assessment_run_service, placement_alarm_service,
        # project_builder and recruiter_attack_service already apply to their generated text.
        # This module was the one AI caller that validated the *shape* of the model's output but
        # never its *content*, so a model that echoed its own prompt back produced a perfectly
        # schema-valid InterviewQuestion whose text was this app's security rules -- persisted,
        # and then read out to the student as their next interview question.
        #
        # Distinct from render_prompt/neutralize_delimiters above, which stop untrusted input
        # from escaping its block on the way IN. Both are needed; neither substitutes for the
        # other. Leaked output is raised as a generation failure so it takes the same route as
        # every other one: never shown to anyone, never persisted.
        if (
            not generated.question_text.strip()
            or looks_like_prompt_leak(generated.question_text)
            or looks_like_prompt_leak(generated.based_on)
        ):
            raise AIGenerationError("Interview question output was empty or leaked prompt scaffolding")
        question_text, based_on = generated.question_text, generated.based_on
    except AIGenerationError as exc:
        # AIGenerationError only, and deliberately nothing wider. The provider already converts
        # its own transport failures, timeouts and malformed/unvalidatable output into this one
        # exception, so it is exactly the set of "the model could not give us a usable question"
        # cases, now including a leaked one. A bare `except Exception` here would swallow real
        # programming and database bugs into a plausible-looking interview, which is a far worse
        # outcome than a visible error.
        if not stages.is_simulation(interview.type):
            # Ordinary interviews keep the behaviour they have always had -- this surfaces as
            # the documented 422 the client already knows how to show. They are five questions
            # the student can simply restart; a twelve-question conversation abandoned halfway
            # through is a different matter, which is why only the simulation falls back.
            raise
        stage = stages.stage_for(order_index)
        logger.warning(
            "Simulation question generation failed for interview_id=%s at question %s (%s); "
            "using the deterministic %s fallback",
            interview_id, order_index, type(exc).__name__, stage,
        )
        question_text, based_on = simulation_fallback.question_for(stage, order_index)

    question = InterviewQuestion(
        interview_id=interview_id,
        question_text=question_text,
        order_index=order_index,
        based_on=based_on,
    )
    db.add(question)
    db.commit()
    db.refresh(question)

    return _next_question_response(interview, question, completed=False)


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

    _insert_answer_once(db, interview_question_id, answer_text, transcribed=False)

    return InterviewAnswerRead(interview_question_id=interview_question_id, answer_text=answer_text)


def submit_audio_answer(
    db: Session,
    profile_id: int,
    interview_id: int,
    interview_question_id: int,
    audio_bytes: bytes,
    ai_provider: AIProvider,
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

    answer_text = ai_provider.transcribe(audio_bytes)

    # "Minimize stored voice data" (PROJECT_HANDOFF.md section 6): the transcript is kept,
    # the raw recording is not persisted -- audio_url stays null by design, a documented
    # deviation from literally storing it.
    #
    # Same atomic insert as the text path. This one raced worse: transcription runs before the
    # write, so a concurrent duplicate paid for a Whisper pass and then died on the unique
    # index anyway. It still pays for the transcription, but now it loses cleanly with a 409
    # instead of a 500.
    _insert_answer_once(db, interview_question_id, answer_text, transcribed=True)

    return InterviewAnswerRead(interview_question_id=interview_question_id, answer_text=answer_text)


def synthesize_question_audio(
    db: Session, profile_id: int, interview_id: int, question_id: int, ai_provider: AIProvider
) -> bytes:
    _get_owned_interview(db, interview_id, profile_id)

    question = db.get(InterviewQuestion, question_id)
    if question is None or question.interview_id != interview_id:
        raise InterviewNotFoundError("Interview question not found")

    return ai_provider.synthesize_speech(question.question_text)


# The evaluation prompt opens "You are evaluating a completed {interview_type} interview", so a
# raw type value has to read as English there. Every pre-existing type already does; the new one
# does not, and "a completed full_simulation interview" tells the grader less than the real
# thing does about what it is looking at. The prompt file itself is unchanged -- only the word
# substituted into it.
_EVALUATION_TYPE_LABELS = {stages.SIMULATION_TYPE: "full recruiter simulation"}


def _build_evaluation_prompt(interview_type: str, mode: str, context: dict, transcript: str) -> str:
    template = _EVALUATION_PROMPT_PATH.read_text(encoding="utf-8")
    return render_prompt(
        template,
        interview_type=_EVALUATION_TYPE_LABELS.get(interview_type, interview_type),
        mode=mode, role_name=context["role_name"], jd_text=context["jd_text"],
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
        # Derived, never stored: an ungraded evaluation is exactly one whose scores were never
        # written, so there is no second piece of state that could drift out of step with the
        # scores themselves. Same shape as RecruiterAttackResultsRead.used_fallback, named for
        # what it says rather than how it came about -- in this phase "fallback" already means
        # the deterministic *question* bank, and one word for two ideas invites exactly the
        # confusion this flag exists to prevent.
        is_graded=evaluation.overall_score is not None,
    )


# What an ungraded evaluation says. Fixed, provider-agnostic, and written for a student: it
# never carries the provider's name, the model's error, a stack trace, or any part of the
# model's output (a leaked evaluation is a failure too, and its text must not reach anyone).
_UNGRADED_FEEDBACK = (
    "This interview is complete, but the automatic review didn't finish, so there's no score "
    "for it yet. Your answers are saved -- open your results again to try the review once more."
)


def _insert_evaluation_once(
    db: Session, evaluation: InterviewEvaluation, interview_id: int
) -> Optional[InterviewEvaluation]:
    """Insert this interview's first and only evaluation row.

    Returns None when the insert landed, and the row a concurrent request inserted first when
    it did not -- in which case the caller must use that row and stop, rather than re-attempting
    anything of its own.

    Both callers reach this after a "does an evaluation already exist?" read that found none,
    and two requests can pass that read before either commits. What decides the winner is
    interview_evaluations.interview_id's unique index, not that earlier read, so the loser's
    INSERT hits the index -- and before this, the IntegrityError escaped uncaught and became a
    500 on a student asking for the result of an interview they had just finished. Same race,
    same resolution, and the same reasoning as _insert_answer_once above; the recovery itself is
    shaped like mission_service.get_or_create_today's, whose lazy create-on-read this shares.

    Two details of the recovery are load-bearing. The rollback has to come first, because the
    failed INSERT leaves the transaction unusable until it does, so the re-read cannot run
    before it. And the re-read is what produces the returned row: the rollback detaches the
    pending object this call tried to insert, which has no id and was never stored, so only the
    winner's row is real.

    Narrow in two ways. It guards this one INSERT rather than the evaluation flow around it.
    And it treats an IntegrityError as the race only if the winner's row is actually there
    afterwards -- if the re-read finds nothing, whatever constraint fired was not this one, and
    that is a bug to surface rather than to hide behind a plausible-looking evaluation.
    """
    db.add(evaluation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = (
            db.query(InterviewEvaluation)
            .filter(InterviewEvaluation.interview_id == interview_id)
            .first()
        )
        if winner is None:
            raise
        logger.info(
            "Evaluation insert for interview_id=%s lost the create race with a concurrent "
            "request -- returning the evaluation that request stored",
            interview_id,
        )
        return winner
    db.refresh(evaluation)
    return None


def _grade_ungraded_row(
    db: Session, existing: InterviewEvaluation, interview_id: int, graded_values: Dict[str, object]
) -> bool:
    """Fill in an interview's existing UNGRADED evaluation, and report whether this call is the
    one that did it.

    True means this call performed the transition, and with it owes the Interview Memory loop
    its single run. False means a concurrent retry got there first -- `existing` has been
    refreshed from the winner's persisted row, so the caller returns that and does nothing else.

    An atomic conditional UPDATE, not "load, mutate, commit". Same primitive, for the same class
    of race, as recruiter_attack_service.answer_turn, mission_service.complete and
    assessment_run_service.submit_answer. Two retries of the same ungraded evaluation both read
    `overall_score IS NULL` before either writes, so no Python-side check can separate them: by
    the time either acts, both have already decided they may. What separates them is that this
    statement's own WHERE re-tests that condition at write time, inside the database, where the
    two writes are ordered against each other -- so exactly one of them finds the row still NULL
    and the other matches nothing. None of that lives in this process, which is why it holds
    across threads, workers, processes and API instances alike.

    `overall_score` serves as the claim flag rather than a column added for the purpose: it is
    already the single source of truth for "ungraded" (the model says so, and the API's
    is_graded is derived from it), and clamp_unit_score always returns a real float, so the
    winning UPDATE necessarily lifts it out of NULL and closes the door behind itself.

    The refresh after the commit is what makes the loser's answer honest -- it returns the row
    the database actually holds, which is the winner's, rather than the scores this call
    computed and never stored.
    """
    result = db.execute(
        update(InterviewEvaluation)
        .where(
            InterviewEvaluation.interview_id == interview_id,
            InterviewEvaluation.overall_score.is_(None),
        )
        .values(**graded_values)
    )
    db.commit()
    db.refresh(existing)
    return bool(result.rowcount)


def _persist_ungraded(
    db: Session, interview_id: int, existing: Optional[InterviewEvaluation]
) -> InterviewEvaluation:
    """Record that grading was attempted and did not produce a result.

    Every score column stays NULL. Nothing is substituted, defaulted or estimated -- a number
    here would not merely be shown to the student, it would be read by Interview Memory and the
    Evidence Engine and would move their Career Twin readiness on the strength of a response
    that was never a judgement of them.

    Writing a row at all (rather than raising) is what lets the client say something true. The
    alternative -- no row -- is indistinguishable from "not evaluated yet", so the student would
    be shown a generic error for a finished interview with no way to tell the two apart.

    Reuses the interview's one evaluation row when there already is one, because
    interview_evaluations.interview_id is unique and the product means exactly one evaluation
    per interview. A retry therefore regrades in place; it never accumulates rows.
    """
    if existing is None:
        fresh = InterviewEvaluation(interview_id=interview_id, feedback_text=_UNGRADED_FEEDBACK)
        winner = _insert_evaluation_once(db, fresh, interview_id)
        # Losing the create race here costs nothing: this call has no grade to lose, and the
        # concurrent request's row is this interview's one evaluation whether it is graded or
        # ungraded. Returning it unchanged is strictly better than overwriting a real grade
        # with this call's "not graded".
        return winner if winner is not None else fresh
    existing.feedback_text = _UNGRADED_FEEDBACK
    db.commit()
    db.refresh(existing)
    return existing


def get_evaluation(
    db: Session, profile_id: int, interview_id: int, ai_provider: AIProvider
) -> InterviewEvaluationRead:
    interview = _get_owned_interview(db, interview_id, profile_id)
    if interview.status != "completed":
        raise InterviewNotCompleteError("Interview is not complete yet")

    existing = db.query(InterviewEvaluation).filter(InterviewEvaluation.interview_id == interview_id).first()
    # Only a real grade is cached. An ungraded row must NOT short-circuit here, or the honest
    # "try the review again" it shows the student would be a promise the endpoint could never
    # keep -- the identical trap recruiter_attack_service._fallback_feedback documents, where a
    # placeholder result permanently blocked any regrade.
    if existing is not None and existing.overall_score is not None:
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
    prompt = _build_evaluation_prompt(interview.type, interview.mode, context, transcript)
    try:
        generated: GeneratedInterviewEvaluation = ai_provider.generate_structured(
            prompt, GeneratedInterviewEvaluation
        )
        # Checked before anything is persisted, so a leaked evaluation can never become a stored
        # row -- which would not merely show the student prompt scaffolding as their feedback,
        # but would feed its scores into Interview Memory and the Evidence Engine via the loop
        # below, moving Career Twin readiness off a response that was never a judgement of them.
        if not generated.feedback.strip() or looks_like_prompt_leak(generated.feedback):
            raise AIGenerationError("Interview evaluation output was empty or leaked prompt scaffolding")
    except AIGenerationError as exc:
        # AIGenerationError only -- the same narrow catch as question generation, and for the
        # same reason: a wider one would turn a genuine bug into a permanently ungradeable
        # interview instead of a visible failure.
        #
        # There is deliberately NO deterministic fallback evaluation. A question can be
        # substituted because any sensible question still produces a real answer to grade; a
        # score cannot, because a substituted number would be a measurement of nothing. The
        # interview becomes honestly ungraded instead, and note what this path does NOT reach:
        # the clamp-and-persist block below, and therefore the Interview Memory loop at the end.
        # Nothing numeric is written, so nothing numeric can be learned from it.
        logger.warning(
            "Interview evaluation failed for interview_id=%s (%s) -- persisting the ungraded "
            "state with every score left NULL; a later call will regrade it",
            interview_id, type(exc).__name__,
        )
        return _evaluation_read(_persist_ungraded(db, interview_id, existing))

    filler_word_rate = None
    if interview.mode == "voice":
        answer_texts = [a.answer_text for a in answers_by_question_id.values() if a.answer_text]
        if answer_texts:
            filler_word_rate = _filler_word_rate(" ".join(answer_texts))

    def _clamped(field_name: str) -> float:
        # Every dimension of GeneratedInterviewEvaluation is documented (in
        # interview_evaluation_v3.txt) as a float from 0.0 to 1.0 -- the schema itself enforces
        # no such bound, so this is the actual enforcement before anything is persisted or fed
        # into Interview Memory below.
        return clamp_unit_score(
            getattr(generated, field_name), field_name=field_name, context=f"interview evaluation, interview_id={interview_id}"
        )

    # Computed once and then applied as a whole, either as the INSERT's values or as the
    # conditional UPDATE's, so the two paths cannot grade an interview differently.
    graded_values: Dict[str, object] = {
        "technical_correctness": _clamped("technical_correctness"),
        "relevance": _clamped("relevance"),
        "completeness": _clamped("completeness"),
        "reasoning": _clamped("reasoning"),
        "structure": _clamped("structure"),
        "clarity": _clamped("clarity"),
        # grammar is a text-mode field per the doc's schema; voice answers are judged on
        # speaking_pace/filler_word_rate instead.
        "grammar": _clamped("grammar") if interview.mode == "text" else None,
        # speaking_pace would need audio duration, which isn't available since raw audio is
        # never persisted -- left None for V1, see README.
        "speaking_pace": None,
        "filler_word_rate": filler_word_rate,
        "role_jd_coverage": _clamped("role_jd_coverage"),
        "overall_score": _clamped("overall_score"),
        "feedback_text": generated.feedback,
    }

    # This interview gets exactly one evaluation row -- interview_evaluations.interview_id is
    # unique, and the product means one evaluation per interview. Which of the two ways of
    # reaching it applies depends on whether an earlier ungraded attempt already left the row
    # behind, and both are decided by the database rather than by the read at the top of this
    # function, because concurrent requests can pass that read together.
    if existing is None:
        evaluation = InterviewEvaluation(interview_id=interview_id, **graded_values)
        winner = _insert_evaluation_once(db, evaluation, interview_id)
        if winner is not None:
            # Lost the create race, so this call's scores were never stored and must not be:
            # the row belongs to the request that got there first. Returning here is also what
            # keeps the Interview Memory loop below from running a second time for the same
            # interview and teaching it the same dimensions twice. Nothing is lost that stays
            # lost -- if the winner's row is ungraded, the retry path below regrades it in
            # place on the next request, which is precisely what an ungraded row exists for.
            return _evaluation_read(winner)
    else:
        evaluation = existing
        if not _grade_ungraded_row(db, existing, interview_id, graded_values):
            # Lost the ungraded -> graded transition to a concurrent retry. The same rule as
            # above, and it is the one that matters most here: the interview has been graded
            # once, so Interview Memory must learn from it once. A loser that carried on would
            # record every dimension of this one interview a second time, inflating the
            # occurrence count the weakness status machine advances on -- and a student would
            # be told a weakness keeps recurring on the strength of having pressed retry twice.
            return _evaluation_read(existing)

    # Interview Memory: every dimension of every completed evaluation updates its persisted
    # cross-interview record (see weakness_record_service's status machine). Deliberately
    # after the evaluation's own commit above -- a failure here must never roll back or block
    # the evaluation the student is waiting on.
    for dimension in INTERVIEW_DIMENSIONS:
        weakness_record_service.record_or_update(
            db, profile_id, f"interview:{dimension}", getattr(evaluation, dimension)
        )

    return _evaluation_read(evaluation)
