"""Recruiter Attack -- a skeptical AI "recruiter" fires a bounded series of pointed follow-up
questions at ONE specific, real claim the student has made, then grades how well they defended
it. Two deliberate scope decisions, both documented here rather than left implicit:

1. The claim under attack must be real and verifiable, never a freeform unverified assertion:
   either a completed ProjectBuild (owned by the student, status == "completed") or a
   substring the student picks out of their own uploaded resume (checked against the actual
   resume text -- see start_session). This mirrors the codebase's "never fabricate" rule one
   level up: it's not just that generated *text* must be grounded in real data, the *subject*
   of the questioning must be too.
2. Grading is session-level only (technical_defense/project_ownership/specificity/
   communication + one overall_score + feedback), not a separate numeric score per turn --
   the same "score the transcript as a whole" convention interview_service.get_evaluation
   already uses, rather than inventing a second grading shape.

Turn generation and answering follow interview_service's exact shape (get_next_question /
submit_answer), including the lazy-eval-then-cache pattern for the final evaluation
(get_evaluation). Unlike interview_service, both the question-generation and evaluation AI
calls here have an explicit deterministic fallback and never raise AIGenerationError to the
caller -- a session must never get stuck, or fail to produce results, just because the AI
call failed or produced an unusable/leaked response.

On completion, every dimension feeds Interview Memory via weakness_record_service (same
wiring interview_service uses, just under a "recruiter_attack:" skill_key prefix instead of
"interview:" -- WeaknessRecord.skill_key is a plain string with no namespace coupling, so this
extends Interview Memory rather than duplicating it). Evidence Engine gets its own new source
function (see evidence_engine._from_recruiter_attack) reading directly from these tables.
"""

import datetime
import logging
from pathlib import Path
from typing import List, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
from app.ai.output_guard import contains_prompt_leak
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.recruiter_attack import GeneratedRecruiterAttackEvaluation, GeneratedRecruiterAttackQuestion
from app.ai.score_utils import clamp_unit_score
from app.models.profile import StudentProfile
from app.models.project_build import ProjectBuild
from app.models.project_build_stage import ProjectBuildStage
from app.models.recruiter_attack_session import RecruiterAttackSession
from app.models.recruiter_attack_turn import RecruiterAttackTurn
from app.models.resume import Resume
from app.schemas.recruiter_attack import (
    NextRecruiterAttackTurnResponse,
    RecruiterAttackResultsRead,
    RecruiterAttackSessionListItem,
    RecruiterAttackSessionRead,
    RecruiterAttackTurnRead,
)
from app.services import weakness_record_service
from app.services.ai_boundary import outside_transaction
from app.services.project_builder.stages import STAGE_TITLES
from app.services.role_context import resolve_role_context

logger = logging.getLogger(__name__)

# Safety cap, not a product pagination feature (disclosed gap -- see assessment_service.py's
# _MAX_HISTORY_ROWS for the full rationale): protects response size for a long-lived account.
_MAX_HISTORY_ROWS = 200

_QUESTION_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "recruiter_attack_question_v2.txt"
_EVALUATION_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "ai" / "prompts" / "recruiter_attack_evaluation_v2.txt"
)

# Attacking one specific claim needs fewer turns than a full multi-topic interview.
_TURNS_PER_SESSION = 4

_EVALUATION_DIMENSIONS = ("technical_defense", "project_ownership", "specificity", "communication")

# Deterministic fallback questions, used only when the AI call fails or leaks -- indexed by
# turn number so a whole session can still complete even with the AI unavailable throughout.
_FALLBACK_QUESTIONS = [
    ("Walk me through exactly what you personally built or did here.", "Your specific role"),
    ("What was the hardest technical decision, and why did you choose that approach?", "Technical decision"),
    ("If I asked a teammate, would they describe your contribution the same way?", "Ownership check"),
    ("What would you change if you rebuilt this today, and why?", "Depth check"),
]


class RecruiterAttackError(Exception):
    pass


class RecruiterAttackNotFoundError(RecruiterAttackError):
    """Session (or a referenced turn) not found, or not owned by this profile."""


class InvalidSourceError(RecruiterAttackError):
    """Bad start-session request: an unowned/incomplete project, or a claim that doesn't
    actually appear on the student's resume."""


class TurnNotPendingError(RecruiterAttackError):
    """This turn has already been answered."""


class RecruiterAttackNotCompleteError(RecruiterAttackError):
    """Results requested before every turn has been answered."""


def _get_owned_session(db: Session, profile_id: int, session_id: int) -> RecruiterAttackSession:
    session = db.get(RecruiterAttackSession, session_id)
    if session is None or session.profile_id != profile_id:
        raise RecruiterAttackNotFoundError("Recruiter attack session not found")
    return session


def _get_turns(db: Session, session_id: int) -> List[RecruiterAttackTurn]:
    return (
        db.query(RecruiterAttackTurn)
        .filter(RecruiterAttackTurn.session_id == session_id)
        .order_by(RecruiterAttackTurn.order_index)
        .all()
    )


def _turn_read(turn: RecruiterAttackTurn) -> RecruiterAttackTurnRead:
    return RecruiterAttackTurnRead(
        id=turn.id, order_index=turn.order_index, question_text=turn.question_text,
        based_on=turn.based_on, answer_text=turn.answer_text,
    )


def _session_read(session: RecruiterAttackSession) -> RecruiterAttackSessionRead:
    return RecruiterAttackSessionRead(
        id=session.id, source_type=session.source_type, source_label=session.source_label,
        status=session.status, created_at=session.created_at, completed_at=session.completed_at,
    )


def _session_list_item(session: RecruiterAttackSession) -> RecruiterAttackSessionListItem:
    return RecruiterAttackSessionListItem(
        **_session_read(session).model_dump(), overall_score=session.overall_score,
    )


def _role_name(db: Session, profile: StudentProfile) -> str:
    context = resolve_role_context(db, profile)
    return context.display_role_name if context is not None else "the target role"


def start_session(
    db: Session,
    profile: StudentProfile,
    source_type: str,
    project_build_id: Optional[int],
    claim_text: Optional[str],
) -> RecruiterAttackSessionRead:
    source_project_build_id: Optional[int] = None

    if source_type == "project_build":
        if project_build_id is None:
            raise InvalidSourceError("project_build_id is required for source_type 'project_build'")
        build = db.get(ProjectBuild, project_build_id)
        if build is None or build.profile_id != profile.id:
            raise InvalidSourceError("Project build not found")
        if build.status != "completed":
            raise InvalidSourceError("Only a completed project can be attacked")
        source_label = build.title
        source_project_build_id = build.id
    elif source_type == "resume_claim":
        claim = (claim_text or "").strip()
        if not claim:
            raise InvalidSourceError("Enter a claim from your resume")
        resume = (
            db.query(Resume)
            .filter(Resume.profile_id == profile.id, Resume.parse_status == "completed")
            .order_by(Resume.id.desc())
            .first()
        )
        if resume is None:
            raise InvalidSourceError("Upload and analyze a resume first")
        if claim.lower() not in resume.raw_text.lower():
            raise InvalidSourceError("That claim wasn't found on your resume")
        source_label = claim
    else:
        raise InvalidSourceError(f"Unsupported source_type: {source_type}")

    session = RecruiterAttackSession(
        profile_id=profile.id,
        source_type=source_type,
        source_label=source_label,
        source_project_build_id=source_project_build_id,
        status="in_progress",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return _session_read(session)


def list_sessions(db: Session, profile_id: int) -> List[RecruiterAttackSessionListItem]:
    sessions = (
        db.query(RecruiterAttackSession)
        .filter(RecruiterAttackSession.profile_id == profile_id)
        .order_by(RecruiterAttackSession.created_at.desc())
        .limit(_MAX_HISTORY_ROWS)
        .all()
    )
    return [_session_list_item(s) for s in sessions]


def _claim_context(db: Session, session: RecruiterAttackSession) -> str:
    if session.source_type == "project_build" and session.source_project_build_id is not None:
        build = db.get(ProjectBuild, session.source_project_build_id)
        if build is not None:
            stages = (
                db.query(ProjectBuildStage)
                .filter(ProjectBuildStage.build_id == build.id, ProjectBuildStage.status == "completed")
                .order_by(ProjectBuildStage.order_index)
                .all()
            )
            notes = "\n".join(
                f"{STAGE_TITLES[s.stage_key]}: {s.notes}" for s in stages if s.notes
            ) or "(no notes recorded)"
            return (
                f"Project: {build.title}\n"
                f"Stack: {build.stack_summary or '(not specified)'}\n"
                f"What they say they built (their own notes, stage by stage):\n{notes}"
            )
    return session.source_label


def _prior_turns_text(turns: List[RecruiterAttackTurn]) -> str:
    if not turns:
        return "None yet -- this is the first question."
    return "\n".join(
        f"Q{t.order_index + 1}: {t.question_text}\nA{t.order_index + 1}: {t.answer_text or '(no answer given)'}"
        for t in turns
    )


def get_next_turn(
    db: Session, profile_id: int, session_id: int, ai_provider: AIProvider
) -> NextRecruiterAttackTurnResponse:
    session = _get_owned_session(db, profile_id, session_id)
    if session.status != "in_progress":
        return NextRecruiterAttackTurnResponse(completed=True, turn=None)

    turns = _get_turns(db, session_id)
    pending = next((t for t in turns if t.answer_text is None), None)
    if pending is not None:
        return NextRecruiterAttackTurnResponse(completed=False, turn=_turn_read(pending))

    if len(turns) >= _TURNS_PER_SESSION:
        session.status = "completed"
        session.completed_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
        return NextRecruiterAttackTurnResponse(completed=True, turn=None)

    profile = db.get(StudentProfile, profile_id)
    role_name = _role_name(db, profile) if profile is not None else "the target role"
    claim_context = _claim_context(db, session)

    question_text: Optional[str] = None
    based_on: Optional[str] = None
    # The prompt is rendered from the existing turns before the boundary below, and the count is
    # kept as a plain int because the row written at the end needs it.
    turn_count = len(turns)
    template = _QUESTION_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = render_prompt(
        template,
        turn_number=turn_count + 1, total_turns=_TURNS_PER_SESSION, role_name=role_name,
        claim_context=claim_context, prior_turns=_prior_turns_text(turns),
    )
    try:
        # Outside any transaction (F12-07).
        with outside_transaction(db):
            generated: GeneratedRecruiterAttackQuestion = ai_provider.generate_structured(
                prompt, GeneratedRecruiterAttackQuestion
            )
        if not generated.question_text.strip() or contains_prompt_leak(generated):
            raise AIGenerationError("Recruiter attack question output was empty or leaked prompt scaffolding")
        question_text, based_on = generated.question_text, generated.based_on
    except AIGenerationError as exc:
        logger.warning("Recruiter attack question generation failed for session_id=%s, using fallback: %s", session_id, exc)
        question_text, based_on = _FALLBACK_QUESTIONS[turn_count % len(_FALLBACK_QUESTIONS)]

    turn = RecruiterAttackTurn(
        session_id=session_id, order_index=turn_count, question_text=question_text, based_on=based_on,
    )
    db.add(turn)
    db.commit()
    db.refresh(turn)
    return NextRecruiterAttackTurnResponse(completed=False, turn=_turn_read(turn))


def answer_turn(
    db: Session, profile_id: int, session_id: int, turn_id: int, answer_text: str
) -> RecruiterAttackTurnRead:
    session = _get_owned_session(db, profile_id, session_id)

    turn = db.get(RecruiterAttackTurn, turn_id)
    if turn is None or turn.session_id != session.id:
        raise RecruiterAttackNotFoundError("Recruiter attack turn not found")
    if turn.answer_text is not None:
        raise TurnNotPendingError("This question has already been answered")

    # Atomic conditional UPDATE (not "load, mutate, commit") -- same shape as
    # mission_service.complete / subscription_service._activate_paid / assessment_run_service.
    # submit_answer for the identical class of race: two concurrent answer submissions for the
    # same turn can both pass the "still unanswered" read above before either commits. Only the
    # request whose UPDATE actually flips a still-NULL answer_text row wins; the loser must
    # never silently overwrite the winner's already-persisted answer.
    result = db.execute(
        update(RecruiterAttackTurn)
        .where(RecruiterAttackTurn.id == turn.id, RecruiterAttackTurn.answer_text.is_(None))
        .values(answer_text=answer_text)
    )
    db.commit()
    if not result.rowcount:
        raise TurnNotPendingError("This question has already been answered")
    db.refresh(turn)

    if turn.order_index == _TURNS_PER_SESSION - 1:
        session.status = "completed"
        session.completed_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()

    return _turn_read(turn)


def _fallback_feedback(turns: List[RecruiterAttackTurn]) -> str:
    """The text shown when the AI evaluation could not be produced. Deliberately carries NO
    scores of any kind.

    This used to return a full GeneratedRecruiterAttackEvaluation whose five score fields were
    all set to `0.3 + 0.3 * (answered / total)` -- a completion ratio, not a judgment of the
    defense -- and those numbers were then written to the same columns a real grade uses. A
    prior pass noticed half the problem and stopped Interview Memory from learning from them,
    but three other consumers were still reading them as real grades:
      1. evidence_engine._fetch_profile_sources selects recruiter-attack turns on
         `RecruiterAttackSession.overall_score.isnot(None)`, so the placeholder became a
         *scored evidence source* feeding skill state, Career Twin readiness and Skill
         Passport -- a student who was never graded had their readiness moved by a number
         that meant nothing.
      2. get_results short-circuits on `session.overall_score is not None`, so the fallback's
         own promise ("try again later for a full evaluation") could never be honored: the
         placeholder permanently blocked any regrade.
      3. RecruiterAttackResultsRead had no way to say "this wasn't really graded", so the
         client rendered a completion ratio as a measurement.
    Leaving the score columns NULL fixes all three at once: (1) the evidence filter excludes
    it for free, (2) the short-circuit no longer trips so a retry genuinely regrades, and (3)
    the response's `used_fallback` flag is derived from exactly that NULL-ness.
    """
    answered = sum(1 for t in turns if t.answer_text)
    return (
        "We couldn't generate detailed AI feedback for this session right now. "
        f"You answered {answered}/{len(turns)} questions -- try again later for a full evaluation."
    )


def _results_read(session: RecruiterAttackSession) -> RecruiterAttackResultsRead:
    return RecruiterAttackResultsRead(
        technical_defense=session.technical_defense, project_ownership=session.project_ownership,
        specificity=session.specificity, communication=session.communication,
        overall_score=session.overall_score, feedback=session.feedback_text,
        # Derived, not stored: an ungraded session is exactly one whose scores were never
        # persisted, so there is no second piece of state that could drift out of sync with
        # the scores themselves.
        used_fallback=session.overall_score is None,
    )


def get_results(
    db: Session, profile_id: int, session_id: int, ai_provider: AIProvider
) -> RecruiterAttackResultsRead:
    session = _get_owned_session(db, profile_id, session_id)
    if session.status != "completed":
        raise RecruiterAttackNotCompleteError("This session isn't complete yet")

    if session.overall_score is not None:
        return _results_read(session)

    turns = _get_turns(db, session_id)
    profile = db.get(StudentProfile, profile_id)
    role_name = _role_name(db, profile) if profile is not None else "the target role"
    claim_context = _claim_context(db, session)
    transcript = "\n\n".join(
        f"Q{t.order_index + 1}: {t.question_text}\nA{t.order_index + 1}: {t.answer_text or '(no answer given)'}"
        for t in turns
    )

    template = _EVALUATION_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = render_prompt(template, role_name=role_name, claim_context=claim_context, transcript=transcript)
    try:
        # Outside any transaction (F12-07). The transcript is already rendered into the prompt.
        with outside_transaction(db):
            generated: GeneratedRecruiterAttackEvaluation = ai_provider.generate_structured(
                prompt, GeneratedRecruiterAttackEvaluation
            )
        if not generated.feedback.strip() or contains_prompt_leak(generated):
            raise AIGenerationError("Recruiter attack evaluation output was empty or leaked prompt scaffolding")
    except AIGenerationError as exc:
        logger.warning(
            "Recruiter attack evaluation failed for session_id=%s -- persisting feedback text only, "
            "leaving every score NULL so this session stays ungraded (no Interview Memory update, no "
            "Evidence Engine contribution, and a later call regrades it): %s", session_id, exc,
        )
        # Scores are deliberately NOT written here -- see _fallback_feedback. Only the
        # explanatory text is persisted, so the session remains genuinely ungraded.
        #
        # Re-read after the boundary: the rows this request started from were detached when the
        # evaluation ran outside a transaction, and _get_owned_session re-checks ownership.
        session = _get_owned_session(db, profile_id, session_id)
        session.feedback_text = _fallback_feedback(_get_turns(db, session_id))
        db.commit()
        return _results_read(session)

    # Every dimension of GeneratedRecruiterAttackEvaluation is documented (in
    # recruiter_attack_evaluation_v2.txt) as a float from 0.0 to 1.0 -- the schema enforces no
    # such bound, so this clamps before persisting or feeding Interview Memory below. Only
    # reached on the real-AI path now; the fallback returned above without persisting a score.
    context = f"recruiter attack evaluation, session_id={session_id}"
    # Re-read and ownership re-checked on this side of the boundary, for the same reason.
    session = _get_owned_session(db, profile_id, session_id)
    turns = _get_turns(db, session_id)
    session.technical_defense = clamp_unit_score(generated.technical_defense, field_name="technical_defense", context=context)
    session.project_ownership = clamp_unit_score(generated.project_ownership, field_name="project_ownership", context=context)
    session.specificity = clamp_unit_score(generated.specificity, field_name="specificity", context=context)
    session.communication = clamp_unit_score(generated.communication, field_name="communication", context=context)
    session.overall_score = clamp_unit_score(generated.overall_score, field_name="overall_score", context=context)
    session.feedback_text = generated.feedback
    db.commit()

    # Interview Memory only ever learns from a real AI-graded evaluation. That is now
    # structurally true rather than guarded by a flag: the fallback path returns above without
    # ever reaching this loop, and without leaving a score for anything else to mistake for
    # one either.
    for dimension in _EVALUATION_DIMENSIONS:
        weakness_record_service.record_or_update(
            db, profile_id, f"recruiter_attack:{dimension}", getattr(session, dimension)
        )

    return _results_read(session)
