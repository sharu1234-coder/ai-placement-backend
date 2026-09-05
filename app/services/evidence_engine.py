"""Evidence Engine -- the shared substrate for Career Twin and Skill Passport. Aggregates, per
skill, every real signal the app already has: assessment scores, practice attempts, projects,
interview questions, and resume mentions. Deliberately NOT a new persisted table -- every
underlying fact is already a row somewhere (AssessmentResult, QuestionAttempt, Project,
InterviewEvaluation, ResumeAnalysis); a separate "evidence event" table would be a second,
driftable source of truth. Computed live at read time, same philosophy as
career_path/service.py's job-readiness computation and progress_service's dashboard.

A user claiming a skill (e.g. a plain profile Skill row) is deliberately NOT one of the sources
below -- a claim must never, by itself, produce SUPPORTED/STRONG/VERIFIED evidence (see the
product's "user claim != verified skill" rule). Nothing here calls AI: every signal is our own
deterministic product logic, exactly like skill_matching.skill_present and
weakness_service.compute_weaknesses, both of which this module reuses rather than duplicates.
"""

import datetime
from dataclasses import dataclass
from typing import List, Optional, Tuple

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.assessment import Assessment
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.career_path_project import CareerPathProject
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.interview_question import InterviewQuestion
from app.models.project import Project
from app.models.project_build import ProjectBuild
from app.models.question import Question
from app.models.question_attempt import QuestionAttempt
from app.models.recruiter_attack_session import RecruiterAttackSession
from app.models.recruiter_attack_turn import RecruiterAttackTurn
from app.services.skill_matching import NoResumeAnalysisError, latest_resume_skills, skill_present

# A skill is "stale" if every piece of evidence for it predates this window -- old enough that
# it's no longer safe to imply the student's ability is current (e.g. an assessment from 8
# months ago). Only applied when at least one dated item exists; undated evidence (a plain
# profile Project, a resume mention -- neither carries its own timestamp) never triggers this.
_STALE_DAYS = 90

# A scored piece of evidence counts as "strong" on its own if it clears this bar.
_STRONG_SCORE_THRESHOLD = 75.0


class EvidenceItem(BaseModel):
    source: str  # assessment | practice | project | interview | recruiter_attack | resume
    source_id: Optional[int] = None
    label: str
    score: Optional[float] = None  # 0-100, when the source produces a real score
    detected_at: Optional[datetime.datetime] = None
    # True when the student simply *told us* this, with nothing on our side confirming it: a
    # keyword on an uploaded resume, or a free-text Project row they typed in themselves.
    # Distinct from `score is None` -- a completed Project Builder build is also unscored, but
    # the student actually did the work and marked it done, so it is NOT self-asserted. This
    # is what keeps _compute_state_and_confidence from promoting pure claims (see there).
    self_asserted: bool = False


class SkillEvidenceView(BaseModel):
    skill: str
    state: str  # claimed | supported | strong | verified | stale
    confidence: float  # 0-1
    # The coarse band `confidence` actually represents. `confidence` only ever takes five
    # discrete values (see _CONFIDENCE_BANDS), so rendering it as a precise percentage
    # ("70% confident") claims a measurement resolution this engine does not have. Clients
    # should show this band; the numeric confidence stays for ordering and bar lengths.
    confidence_band: str  # none | low | moderate | high | very_high
    items: List[EvidenceItem]
    # The newest timestamp among evidence the student did NOT simply assert about themselves.
    # A resume keyword or a typed-in project row is a claim, not a verification, so it must
    # never set this field -- "Last verified" has to mean something was actually observed.
    # None when nothing independent has been seen yet, even if self-asserted items exist.
    last_verified_at: Optional[datetime.datetime] = None


@dataclass
class _ProfileEvidenceSources:
    """Every row this profile could possibly match evidence against, fetched exactly ONCE per
    get_skill_evidence/get_all_skill_evidence call. Before this, each of the six sources below
    ran its own query PER SKILL CHECKED -- a genuine N+1 (roughly 9 queries x N skills for a
    role's full skill list, since none of these queries actually filter by skill in SQL; only
    the _match_* functions below do that, in Python, against whatever this profile has). Since
    the underlying rows never depend on which skill is being checked, fetching once and
    matching every skill against the same in-memory rows is behavior-identical -- same rows,
    same skill_present matching, same "most-recent/first match wins" ordering -- and turns an
    O(N) query count into O(1) for the batch form Career Twin/Skill Passport actually use."""

    assessment_results: List[AssessmentResult]
    practice_rows: List[Tuple[QuestionAttempt, str, int]]
    projects: List[Project]
    completed_career_path_projects: List[CareerPathProject]
    completed_project_builds: List[ProjectBuild]
    interview_rows: List[Tuple[InterviewQuestion, InterviewEvaluation]]
    recruiter_attack_rows: List[Tuple[RecruiterAttackTurn, RecruiterAttackSession]]
    resume_skills: Optional[List[str]]  # None means no completed resume analysis exists


def _fetch_profile_sources(db: Session, profile_id: int) -> _ProfileEvidenceSources:
    assessment_results = (
        db.query(AssessmentResult)
        .join(Assessment, Assessment.id == AssessmentResult.assessment_id)
        .filter(Assessment.profile_id == profile_id)
        .order_by(AssessmentResult.created_at.desc())
        .all()
    )
    practice_rows = (
        db.query(QuestionAttempt, Question.skill_tag, QuestionAttempt.assessment_id)
        .join(Question, Question.id == QuestionAttempt.question_id)
        .join(Assessment, Assessment.id == QuestionAttempt.assessment_id)
        .filter(Assessment.profile_id == profile_id, QuestionAttempt.student_answer.isnot(None))
        .all()
    )
    projects = db.query(Project).filter(Project.profile_id == profile_id).all()
    completed_career_path_projects = (
        db.query(CareerPathProject)
        .join(CareerPath, CareerPath.id == CareerPathProject.career_path_id)
        .filter(CareerPath.profile_id == profile_id, CareerPathProject.status == "completed")
        .all()
    )
    completed_project_builds = (
        db.query(ProjectBuild).filter(ProjectBuild.profile_id == profile_id, ProjectBuild.status == "completed").all()
    )
    interview_rows = (
        db.query(InterviewQuestion, InterviewEvaluation)
        .join(Interview, Interview.id == InterviewQuestion.interview_id)
        .join(InterviewEvaluation, InterviewEvaluation.interview_id == Interview.id)
        .filter(Interview.profile_id == profile_id)
        .order_by(InterviewEvaluation.created_at.desc())
        .all()
    )
    recruiter_attack_rows = (
        db.query(RecruiterAttackTurn, RecruiterAttackSession)
        .join(RecruiterAttackSession, RecruiterAttackSession.id == RecruiterAttackTurn.session_id)
        .filter(RecruiterAttackSession.profile_id == profile_id, RecruiterAttackSession.overall_score.isnot(None))
        .order_by(RecruiterAttackSession.completed_at.desc())
        .all()
    )
    try:
        resume_skills = latest_resume_skills(db, profile_id)
    except NoResumeAnalysisError:
        resume_skills = None

    return _ProfileEvidenceSources(
        assessment_results=assessment_results,
        practice_rows=practice_rows,
        projects=projects,
        completed_career_path_projects=completed_career_path_projects,
        completed_project_builds=completed_project_builds,
        interview_rows=interview_rows,
        recruiter_attack_rows=recruiter_attack_rows,
        resume_skills=resume_skills,
    )


def _match_assessment(sources: _ProfileEvidenceSources, skill: str) -> Optional[EvidenceItem]:
    for result in sources.assessment_results:
        for tag, raw_score in result.skill_scores.items():
            if not skill_present(skill, [tag]):
                continue
            score = round(raw_score * 100, 1)
            return EvidenceItem(
                source="assessment",
                source_id=result.assessment_id,
                label=f"Assessment — {round(score)}%",
                score=score,
                detected_at=result.created_at,
            )
    return None


def _match_practice(sources: _ProfileEvidenceSources, skill: str) -> Optional[EvidenceItem]:
    matching = [attempt for attempt, tag, _assessment_id in sources.practice_rows if skill_present(skill, [tag])]
    if not matching:
        return None

    correct = sum(1 for a in matching if a.is_correct)
    score = round(100 * correct / len(matching), 1)

    # QuestionAttempt has no timestamp of its own -- best-effort dating via the most recent
    # AssessmentResult among the matching attempts' assessments (may legitimately be None if
    # none of those assessments has finished yet). sources.assessment_results is already
    # ordered created_at desc (see _fetch_profile_sources), so the first match found here by
    # simple iteration is also the most recent -- same result the original per-skill query
    # (`.filter(assessment_id.in_(...)).order_by(created_at.desc()).first()`) computed, without
    # a second query.
    assessment_ids = {attempt.assessment_id for attempt in matching}
    latest_result = next((r for r in sources.assessment_results if r.assessment_id in assessment_ids), None)
    return EvidenceItem(
        source="practice",
        source_id=None,
        label=f"Practice — {len(matching)} problem{'s' if len(matching) != 1 else ''}",
        score=score,
        detected_at=latest_result.created_at if latest_result else None,
    )


def _match_projects(sources: _ProfileEvidenceSources, skill: str) -> Optional[EvidenceItem]:
    # Flat profile-level projects (manual entries / resume-extracted) -- same matching
    # approach as career_path/service.py's _compute_resume_connection._project_matches.
    for project in sources.projects:
        blob = " ".join(filter(None, [project.title, project.description, project.tech_stack]))
        if skill_present(skill, [blob]):
            # Free text the student typed themselves (POST /users/me/projects), with no status
            # field and nothing verifying it -- a claim, unlike the two completed-work branches
            # below.
            return EvidenceItem(
                source="project", source_id=project.id, label=f"Project — {project.title}", self_asserted=True
            )

    # Completed Career Path project recommendations -- only counts once the student has
    # actually marked it done, unlike the flat Project list above (which has no status field).
    for project in sources.completed_career_path_projects:
        tags = list(project.skills_demonstrated) + list(project.technologies)
        if any(skill_present(skill, [tag]) for tag in tags):
            return EvidenceItem(source="project", source_id=project.id, label=f"Project — {project.title}")

    # Completed Project Builder builds -- dated (unlike the two sources above) since a real
    # completed_at timestamp exists, so a guided build genuinely can go stale over time.
    for build in sources.completed_project_builds:
        blob = " ".join(filter(None, [build.title, build.stack_summary]))
        if skill_present(skill, [blob]):
            return EvidenceItem(
                source="project", source_id=build.id, label=f"Project — {build.title}", detected_at=build.completed_at
            )
    return None


def _match_interview(sources: _ProfileEvidenceSources, skill: str) -> Optional[EvidenceItem]:
    # A question's based_on tag (e.g. "DSA", "Project: ETL Pipeline") is the intended,
    # short skill/topic signal; question_text is checked too so a skill can still be matched
    # when it's only mentioned inside the question itself (e.g. "Explain Kafka partitioning").
    # Scored at the interview level (InterviewEvaluation has no per-question breakdown).
    for question, evaluation in sources.interview_rows:
        if not (skill_present(skill, [question.based_on]) or skill_present(skill, [question.question_text])):
            continue
        score = round(evaluation.overall_score * 100, 1)
        return EvidenceItem(
            source="interview",
            source_id=evaluation.interview_id,
            label=f"Interview — {round(score)}%",
            score=score,
            detected_at=evaluation.created_at,
        )
    return None


def _match_recruiter_attack(sources: _ProfileEvidenceSources, skill: str) -> Optional[EvidenceItem]:
    # Only sessions with a completed evaluation count (overall_score is set once get_results
    # has run, same lazy-eval-then-cache shape as InterviewEvaluation). Matched against the
    # turn's own based_on/question_text first (most specific), falling back to the session's
    # source_label (the project title or resume claim under attack) -- same dual-check
    # reasoning as _match_interview.
    for turn, session in sources.recruiter_attack_rows:
        if not (
            skill_present(skill, [turn.based_on])
            or skill_present(skill, [turn.question_text])
            or skill_present(skill, [session.source_label])
        ):
            continue
        score = round(session.overall_score * 100, 1)
        return EvidenceItem(
            source="recruiter_attack", source_id=session.id, label=f"Recruiter Attack — {round(score)}%",
            score=score, detected_at=session.completed_at,
        )
    return None


def _match_resume(sources: _ProfileEvidenceSources, skill: str) -> Optional[EvidenceItem]:
    if sources.resume_skills is None:
        return None
    if skill_present(skill, sources.resume_skills):
        return EvidenceItem(
            source="resume", source_id=None, label="Resume evidence detected", self_asserted=True
        )
    return None


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    """SQLite (used in tests and, per app/database/session.py, potentially elsewhere) has no
    native timezone-aware datetime type -- a value written as tz-aware comes back naive on
    read. Treat a naive value as UTC (every created_at default in this codebase already uses
    datetime.now(timezone.utc), so that's the only timezone a naive value could mean) rather
    than let a naive/aware subtraction raise."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)


# Every state's coarse confidence band, keyed so it cannot drift from the numeric confidence
# returned alongside it in _compute_state_and_confidence. `confidence` is not a measurement on
# a continuous scale -- it takes exactly five values, one per state -- so any UI that renders
# it as a precise percentage is overstating what this engine knows. Clients show the band.
_CONFIDENCE_BANDS = {
    "claimed": "none",
    "stale": "low",
    "supported": "moderate",
    "strong": "high",
    "verified": "very_high",
}


def _compute_state_and_confidence(items: List[EvidenceItem]) -> tuple[str, float]:
    """A documented, deterministic rule -- not a black box. verified requires *both* breadth
    (3+ independent sources) and depth (at least one strongly-scored source); strong requires
    breadth AND at least one source the student did not simply assert about themselves;
    supported is a single real source, or any number of purely self-asserted ones; claimed is
    zero. stale overrides all of the above once every dated piece of evidence has aged past
    _STALE_DAYS (undated evidence, e.g. a plain project listing with no timestamp, never
    triggers staleness on its own).

    The self-asserted requirement on `strong` exists because breadth alone was satisfiable
    entirely by claims: a keyword on the uploaded resume plus a free-text Project row the
    student typed in are two *different sources* but zero actual proof, and that combination
    used to yield "strong / 70% confidence" plus a "{skill} is now Strong!" push notification.
    That contradicted both this module's own rule that a claim must never by itself produce
    supported/strong/verified evidence, and the Skill Passport's promise to students: "Proof
    that you can actually perform each skill -- not just a claim." Note `verified` was never
    exposed to this, since has_strong_score already requires a scored (therefore
    non-self-asserted) source."""
    if not items:
        return "claimed", 0.0

    dated_items = [item for item in items if item.detected_at is not None]
    if dated_items:
        now = datetime.datetime.now(datetime.timezone.utc)
        if all((now - _as_utc(item.detected_at)).days > _STALE_DAYS for item in dated_items):
            return "stale", 0.3

    sources = {item.source for item in items}
    has_strong_score = any(item.score is not None and item.score >= _STRONG_SCORE_THRESHOLD for item in items)

    has_independent_source = any(not item.self_asserted for item in items)

    if len(sources) >= 3 and has_strong_score:
        return "verified", 0.9
    if len(sources) >= 2 and has_independent_source:
        return "strong", 0.7
    return "supported", 0.45


def _evidence_view_from_sources(sources: _ProfileEvidenceSources, skill: str) -> SkillEvidenceView:
    items = [
        item
        for item in (
            _match_assessment(sources, skill),
            _match_practice(sources, skill),
            _match_projects(sources, skill),
            _match_interview(sources, skill),
            _match_recruiter_attack(sources, skill),
            _match_resume(sources, skill),
        )
        if item is not None
    ]
    state, confidence = _compute_state_and_confidence(items)
    # Only independently-observed evidence can date a *verification*. Previously this took
    # max() over every dated item, so a resume upload -- a claim the student typed -- set
    # "Last verified", and the Skill Passport showed a verification date for a skill nothing
    # had ever verified.
    verified_dates = [
        item.detected_at
        for item in items
        if item.detected_at is not None and not item.self_asserted
    ]
    return SkillEvidenceView(
        skill=skill,
        state=state,
        confidence=confidence,
        confidence_band=_CONFIDENCE_BANDS[state],
        items=items,
        last_verified_at=max(verified_dates) if verified_dates else None,
    )


def get_skill_evidence(db: Session, profile_id: int, skill: str) -> SkillEvidenceView:
    sources = _fetch_profile_sources(db, profile_id)
    return _evidence_view_from_sources(sources, skill)


def get_all_skill_evidence(db: Session, profile_id: int, skills: List[str]) -> List[SkillEvidenceView]:
    """Batch form for list screens (Career Twin's skill list, Skill Passport's index). Order
    preserved from the input skills list -- callers control ranking (e.g. by role-skill
    weight), this function only computes evidence. Fetches every source row for this profile
    ONCE (see _fetch_profile_sources) and matches every skill in [skills] against that same
    in-memory snapshot, rather than re-querying per skill."""
    sources = _fetch_profile_sources(db, profile_id)
    return [_evidence_view_from_sources(sources, skill) for skill in skills]


# One shared phrasing table for "what should I do about this skill", keyed by evidence state
# -- used by both Career Twin (biggest gap's next action) and Skill Passport (every skill's
# next action), so the two features can never say something different about the same skill.
_NEXT_ACTION_TEMPLATES = {
    "claimed": "Start learning {skill} fundamentals.",
    "supported": "Practice more {skill} problems to strengthen your evidence.",
    "strong": "Keep {skill} sharp with a quick practice round.",
    "verified": "Keep {skill} sharp with a quick practice round.",
    "stale": "Refresh your {skill} evidence — it hasn't been demonstrated recently.",
}


def next_action_hint(skill: str, state: str) -> str:
    return _NEXT_ACTION_TEMPLATES[state].format(skill=skill)
