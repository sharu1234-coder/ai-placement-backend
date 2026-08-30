"""Career Path orchestration service. Deliberately independent of placement_path_service.py
-- imports nothing from it, shares no tables, no progress state, no ViewModel/service
coupling. Only genuinely generic infrastructure is reused: skill_matching, weakness_service,
the Assessment engine's tables (via an independently-written practice-blueprint builder
below, not a call into placement_path_service's), and Interview/InterviewEvaluation data for
the interview-prep summary. A student can use Career Path with zero Roadmap/Placement Path
activity, and vice versa -- neither reads the other's tables.
"""

import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.ai.provider import AIProvider
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.career_path_phase import CareerPathPhase
from app.models.career_path_project import CareerPathProject
from app.models.career_path_skill_item import CareerPathSkillItem
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_description import JobDescription
from app.models.project import Project
from app.models.requirement import Requirement
from app.models.role import Role
from app.schemas.assessment import BlueprintSection
from app.schemas.career_path import (
    CareerPathPhaseRead,
    CareerPathProjectRead,
    CareerPathRead,
    CareerPathSummaryRead,
    InterviewPrepCategorySummaryRead,
    JobReadinessRead,
    ResumeConnectionRead,
    RoleOverviewRead,
    SkillGapItemRead,
    SkillItemRead,
)
from app.services.career_path import ai_generation, rules
from app.services.career_path.ai_generation import ExplainerSkillInput
from app.services.career_path.role_catalog import RoleFamilyDef, get_role_family, resolve_role_family
from app.services.skill_matching import NoResumeAnalysisError, latest_resume_skills, skill_present
from app.services.weakness_service import compute_weaknesses

PRACTICE_QUESTION_COUNT = 3
_BLUEPRINT_SOURCE = "career_path"
_GENERIC_ASSESSMENT_TYPE = "technical"

# Safety cap, not a product pagination feature (disclosed gap -- see assessment_service.py's
# _MAX_HISTORY_ROWS for the full rationale): protects response size for a long-lived account.
_MAX_SAVED_PATHS = 200

_INTERVIEW_CATEGORIES = ["technical", "hr", "project", "behavioral", "role_specific"]
_INTERVIEW_DIMENSION_FIELDS = [
    "technical_correctness",
    "relevance",
    "completeness",
    "reasoning",
    "structure",
    "clarity",
    "role_jd_coverage",
]
_WEAK_DIMENSION_THRESHOLD = 0.6

_FOCUS_AREA_TEMPLATES = {
    "skills": "Complete more skills in your Career Path",
    "projects": "Build a portfolio project for this role",
    "resume": "Update your resume to reflect your {role} skills",
    "practice": "Practice more skill assessments for this role",
    "interview": "Practice interview questions for this role",
}


class CareerPathNotFoundError(Exception):
    """Path not found, or not owned by this profile."""


class RoleNotFoundError(Exception):
    """role_id doesn't exist in the Role catalog."""


class SkillItemNotFoundError(Exception):
    """Skill item not found, or not owned by this profile (via its path)."""


class ProjectNotFoundError(Exception):
    """Project not found, or not owned by this profile (via its path)."""


class JobDescriptionNotFoundError(Exception):
    """JD not found, or not owned by this profile."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Role resolution
# ---------------------------------------------------------------------------


def _resolve_role_name(db: Session, role_id: Optional[int], custom_role: Optional[str]) -> str:
    if role_id is not None:
        role = db.query(Role).filter(Role.id == role_id).first()
        if role is None:
            raise RoleNotFoundError(f"Role {role_id} not found")
        return role.name
    return (custom_role or "").strip()


def _derive_family_key(role_name: str) -> str:
    """The family_key role_name resolves to, computable without any AI call: a curated
    catalog match, or the stable "custom:<slug>" ai_generation would derive for the
    AI-assisted tier. Lets callers look up an already-generated CareerPath row *before*
    deciding whether a fresh AI call is even needed."""
    catalog_key = resolve_role_family(role_name)
    if catalog_key is not None:
        return catalog_key
    return ai_generation.derive_custom_family_key(role_name)


def _resolve_role_family(
    role_name: str,
    family_key: str,
    ai_provider: AIProvider,
    cached_path: Optional[CareerPath],
    force_regenerate: bool,
) -> Tuple[RoleFamilyDef, str]:
    """Returns (role_family, generated_via). For a curated catalog match, always returns the
    static definition (cheap, deterministic, never cached). For the AI-assisted tier, reuses
    cached_path.generated_definition when one already exists and force_regenerate is False --
    AI-generated skill_keys are not guaranteed stable across separate calls, so re-resolving
    from scratch on every ordinary re-selection would silently break carry-forward-by-key (see
    rules.build_phase_specs) and reset the student's progress. Only an explicit refresh()
    (force_regenerate=True) re-calls the AI."""
    if not family_key.startswith("custom:"):
        family = get_role_family(family_key)
        assert family is not None, f"catalog family_key {family_key!r} resolved but missing from ROLE_FAMILIES"
        return family, "catalog"

    if (
        not force_regenerate
        and cached_path is not None
        and cached_path.role_family_key == family_key
        and cached_path.generated_definition
    ):
        family = ai_generation.rehydrate_role_family(
            family_key, cached_path.display_role_name, cached_path.role_overview, cached_path.generated_definition
        )
        return family, cached_path.generated_via

    return ai_generation.get_or_generate_role_definition(role_name, ai_provider)


def _role_overview_dict(role_family: RoleFamilyDef) -> dict:
    return {
        "display_name": role_family.display_name,
        "category": role_family.category,
        "short_explanation": role_family.short_explanation,
        "responsibilities": list(role_family.responsibilities),
        "core_skills": list(role_family.core_skills),
        "tools": list(role_family.tools),
        "difficulty": role_family.difficulty,
        "entry_level_expectations": role_family.entry_level_expectations,
        "interview_focus": list(role_family.interview_focus),
        "portfolio_expectations": role_family.portfolio_expectations,
    }


# ---------------------------------------------------------------------------
# Ownership helpers
# ---------------------------------------------------------------------------


def _get_owned_path(db: Session, profile_id: int, path_id: int) -> CareerPath:
    path = db.query(CareerPath).filter(CareerPath.id == path_id).first()
    if path is None or path.profile_id != profile_id:
        raise CareerPathNotFoundError(f"Career path {path_id} not found")
    return path


def _get_owned_skill_item(db: Session, profile_id: int, item_id: int) -> Tuple[CareerPathSkillItem, CareerPath]:
    item = db.query(CareerPathSkillItem).filter(CareerPathSkillItem.id == item_id).first()
    if item is None:
        raise SkillItemNotFoundError(f"Skill item {item_id} not found")
    path = db.query(CareerPath).filter(CareerPath.id == item.career_path_id).first()
    if path is None or path.profile_id != profile_id:
        raise SkillItemNotFoundError(f"Skill item {item_id} not found")
    return item, path


def _get_owned_project(db: Session, profile_id: int, project_id: int) -> Tuple[CareerPathProject, CareerPath]:
    project = db.query(CareerPathProject).filter(CareerPathProject.id == project_id).first()
    if project is None:
        raise ProjectNotFoundError(f"Project {project_id} not found")
    path = db.query(CareerPath).filter(CareerPath.id == project.career_path_id).first()
    if path is None or path.profile_id != profile_id:
        raise ProjectNotFoundError(f"Project {project_id} not found")
    return project, path


# ---------------------------------------------------------------------------
# Generation / selection
# ---------------------------------------------------------------------------


def _carry_forward_statuses(db: Session, career_path_id: int) -> Tuple[Dict[str, Tuple[str, Optional[str]]], Dict[str, str]]:
    """(skill_key -> (status, completed_via), project_key -> status) for an existing path's
    children, used to preserve completion when refreshing/regenerating a path in place."""
    skill_statuses = {
        item.skill_key: (item.status, item.completed_via)
        for item in db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == career_path_id)
    }
    project_statuses = {
        p.project_key: p.status for p in db.query(CareerPathProject).filter(CareerPathProject.career_path_id == career_path_id)
    }
    return skill_statuses, project_statuses


def _sync_path_children(db: Session, path: CareerPath, role_family: RoleFamilyDef, ai_provider: AIProvider) -> None:
    """Rebuilds a path's phases/skill items/projects from role_family's specs, carrying
    forward completion status by stable key. Safe on both first creation (nothing to carry
    forward) and refresh (existing rows carried forward, then replaced)."""
    skill_statuses, project_statuses = _carry_forward_statuses(db, path.id)

    db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == path.id).delete()
    db.query(CareerPathProject).filter(CareerPathProject.career_path_id == path.id).delete()
    db.query(CareerPathPhase).filter(CareerPathPhase.career_path_id == path.id).delete()
    db.flush()

    phase_specs = rules.build_phase_specs(role_family)
    all_items: List[CareerPathSkillItem] = []

    for phase_spec in phase_specs:
        db.add(
            CareerPathPhase(
                career_path_id=path.id,
                phase_key=phase_spec.phase_key,
                order_index=phase_spec.order_index,
                title=phase_spec.title,
                objective=phase_spec.objective,
            )
        )

        for item_spec in phase_spec.skill_items:
            prior_status, prior_completed_via = skill_statuses.get(item_spec.skill_key, ("not_started", None))
            item = CareerPathSkillItem(
                career_path_id=path.id,
                phase_key=item_spec.phase_key,
                skill_key=item_spec.skill_key,
                title=item_spec.title,
                level=item_spec.level,
                order_index=item_spec.order_index,
                status=prior_status,
                completed_via=prior_completed_via,
            )
            db.add(item)
            all_items.append(item)

        for project_spec in phase_spec.projects:
            prior_status = project_statuses.get(project_spec.project_key, "not_started")
            db.add(
                CareerPathProject(
                    career_path_id=path.id,
                    project_key=project_spec.project_key,
                    title=project_spec.title,
                    difficulty=project_spec.difficulty,
                    skills_demonstrated=project_spec.skills_demonstrated,
                    technologies=project_spec.technologies,
                    problem_statement=project_spec.problem_statement,
                    expected_outcome=project_spec.expected_outcome,
                    what_should_be_included=project_spec.what_should_be_included,
                    why_recruiters_value=project_spec.why_recruiters_value,
                    resume_bullet_suggestion=project_spec.resume_bullet_suggestion,
                    interview_questions=project_spec.interview_questions,
                    order_index=project_spec.order_index,
                    status=prior_status,
                )
            )

    db.flush()

    explainer_inputs = [
        ExplainerSkillInput(skill_key=i.skill_key, title=i.title, phase_key=i.phase_key, level=i.level) for i in all_items
    ]
    explanations = ai_generation.generate_skill_explanations(role_family.display_name, ai_provider, explainer_inputs)
    for item in all_items:
        item.why_it_matters = explanations.get(item.skill_key)

    path.role_overview = _role_overview_dict(role_family)


def select_role(
    db: Session,
    profile_id: int,
    ai_provider: AIProvider,
    role_id: Optional[int] = None,
    custom_role: Optional[str] = None,
) -> CareerPathRead:
    """Selects (or re-selects) a target role: finds-or-creates the CareerPath row for this
    role family, makes it the profile's current/primary path, and (re)builds its content.
    Never touches StudentProfile.target_role_id/custom_target_role. Re-selecting a role that
    already has a generated definition reuses it rather than calling the AI again -- see
    _resolve_role_family."""
    role_name = _resolve_role_name(db, role_id, custom_role)
    family_key = _derive_family_key(role_name)

    path = db.query(CareerPath).filter(CareerPath.profile_id == profile_id, CareerPath.role_family_key == family_key).first()
    role_family, generated_via = _resolve_role_family(role_name, family_key, ai_provider, path, force_regenerate=False)

    if path is None:
        path = CareerPath(
            profile_id=profile_id,
            role_id=role_id,
            custom_role=custom_role,
            role_family_key=family_key,
            display_role_name=role_family.display_name,
            is_primary=False,
            generated_via=generated_via,
            role_overview={},
        )
        db.add(path)
        db.flush()
    else:
        path.role_id = role_id
        path.custom_role = custom_role
        path.display_role_name = role_family.display_name
        path.generated_via = generated_via
        path.updated_at = _now()

    # Selecting a role always makes it the current one.
    db.query(CareerPath).filter(CareerPath.profile_id == profile_id, CareerPath.id != path.id).update({"is_primary": False})
    path.is_primary = True

    _sync_path_children(db, path, role_family, ai_provider)
    if family_key.startswith("custom:"):
        path.generated_definition = ai_generation.serialize_role_family(role_family)
    db.commit()
    db.refresh(path)
    return _to_read(db, path)


def refresh(db: Session, profile_id: int, path_id: int, ai_provider: AIProvider) -> CareerPathRead:
    """Regenerates an existing path's content in place, carrying forward completion status.
    Does NOT change which path is primary. Unlike select_role(), this always re-calls the AI
    for the AI-assisted tier (force_regenerate=True) -- this is the one explicit, user-
    initiated, rate-limited action meant to actually pull fresh content."""
    path = _get_owned_path(db, profile_id, path_id)
    role_name = _resolve_role_name(db, path.role_id, path.custom_role)
    role_family, generated_via = _resolve_role_family(role_name, path.role_family_key, ai_provider, path, force_regenerate=True)
    path.generated_via = generated_via
    path.display_role_name = role_family.display_name
    path.updated_at = _now()
    _sync_path_children(db, path, role_family, ai_provider)
    if path.role_family_key.startswith("custom:"):
        path.generated_definition = ai_generation.serialize_role_family(role_family)
    db.commit()
    db.refresh(path)
    return _to_read(db, path)


# ---------------------------------------------------------------------------
# Gap analysis / resume connection / interview prep / job readiness
# ---------------------------------------------------------------------------


def _compute_skill_gaps(db: Session, profile_id: int, core_skills: List[str], tools: List[str]) -> List[SkillGapItemRead]:
    try:
        resume_skills = latest_resume_skills(db, profile_id)
    except NoResumeAnalysisError:
        resume_skills = []

    weak_tags: List[str] = []
    if resume_skills:
        weaknesses = compute_weaknesses(db, profile_id, jd_id=None)
        weak_tags = [w.skill_tag.lower() for w in weaknesses]

    gaps: List[SkillGapItemRead] = []
    for skill in core_skills:
        present = skill_present(skill, resume_skills)
        is_weak = any(skill_present(skill, [tag]) for tag in weak_tags)
        if present and not is_weak:
            gaps.append(SkillGapItemRead(skill=skill, status="strong", evidence="Present on your resume."))
        elif present and is_weak:
            gaps.append(
                SkillGapItemRead(
                    skill=skill,
                    status="needs_improvement",
                    evidence="On your resume, but flagged as a weak area in recent assessments or interviews.",
                )
            )
        else:
            gaps.append(SkillGapItemRead(skill=skill, status="missing", evidence="Not yet on your resume."))

    core_skills_lower = {s.lower() for s in core_skills}
    for tool in tools:
        if tool.lower() in core_skills_lower:
            continue
        if not skill_present(tool, resume_skills):
            gaps.append(SkillGapItemRead(skill=tool, status="recommended", evidence=None))

    return gaps


def _compute_resume_connection(
    db: Session, profile_id: int, core_skills: List[str], projects_total: int
) -> Optional[ResumeConnectionRead]:
    try:
        resume_skills = latest_resume_skills(db, profile_id)
    except NoResumeAnalysisError:
        return None

    skills_total = len(core_skills)
    skills_demonstrated = sum(1 for s in core_skills if skill_present(s, resume_skills))
    missing_evidence = [s for s in core_skills if not skill_present(s, resume_skills)]

    student_projects = db.query(Project).filter(Project.profile_id == profile_id).all()

    def _project_matches(p: Project) -> bool:
        blob = " ".join(filter(None, [p.title, p.description, p.tech_stack]))
        return any(skill_present(skill, [blob]) for skill in core_skills)

    projects_demonstrated = 0
    if projects_total:
        projects_demonstrated = min(sum(1 for p in student_projects if _project_matches(p)), projects_total)

    role_keyword_coverage_percent = round(100 * skills_demonstrated / skills_total) if skills_total else 0

    return ResumeConnectionRead(
        skills_demonstrated=skills_demonstrated,
        skills_total=skills_total,
        projects_demonstrated=projects_demonstrated,
        projects_total=projects_total,
        role_keyword_coverage_percent=role_keyword_coverage_percent,
        missing_evidence=missing_evidence,
    )


def _compute_interview_prep_summary(db: Session, profile_id: int) -> List[InterviewPrepCategorySummaryRead]:
    """Reads real Interview/InterviewEvaluation data directly (not the shared
    weakness_service aggregator, which pools across categories) so weak_areas can be
    reported per interview category. Every "Practice" action in the UI deep-links into the
    existing mock-interview screens -- this function only ever summarizes, never conducts an
    interview."""
    summaries: List[InterviewPrepCategorySummaryRead] = []
    for category in _INTERVIEW_CATEGORIES:
        interviews = db.query(Interview).filter(Interview.profile_id == profile_id, Interview.type == category).all()
        if not interviews:
            continue

        interview_ids = [i.id for i in interviews]
        evaluations = db.query(InterviewEvaluation).filter(InterviewEvaluation.interview_id.in_(interview_ids)).all()

        accuracy_percent: Optional[int] = None
        weak_areas: List[str] = []
        if evaluations:
            accuracy_percent = round(100 * sum(e.overall_score for e in evaluations) / len(evaluations))
            for dim in _INTERVIEW_DIMENSION_FIELDS:
                values = [getattr(e, dim) for e in evaluations if getattr(e, dim) is not None]
                if values and (sum(values) / len(values)) < _WEAK_DIMENSION_THRESHOLD:
                    weak_areas.append(dim.replace("_", " "))

        summaries.append(
            InterviewPrepCategorySummaryRead(
                category=category,
                questions_completed=len(interviews),
                accuracy_percent=accuracy_percent,
                weak_areas=weak_areas,
            )
        )
    return summaries


def _compute_practice_percent(db: Session, career_path_id: int) -> int:
    blueprint_ids = [
        bid
        for (bid,) in db.query(CareerPathSkillItem.practice_blueprint_id)
        .filter(
            CareerPathSkillItem.career_path_id == career_path_id,
            CareerPathSkillItem.practice_blueprint_id.isnot(None),
        )
        .all()
    ]
    if not blueprint_ids:
        return 0
    scores = [
        score
        for (score,) in db.query(AssessmentResult.overall_score)
        .join(Assessment, Assessment.id == AssessmentResult.assessment_id)
        .filter(Assessment.blueprint_id.in_(blueprint_ids))
        .all()
    ]
    if not scores:
        return 0
    return round(100 * sum(scores) / len(scores))


def _compute_job_readiness(
    role_display_name: str,
    skills_percent: int,
    projects_percent: int,
    resume_percent: int,
    practice_percent: int,
    interview_percent: int,
    is_estimate: bool,
) -> JobReadinessRead:
    overall = round(sum([skills_percent, projects_percent, resume_percent, practice_percent, interview_percent]) / 5)
    scores = {
        "skills": skills_percent,
        "projects": projects_percent,
        "resume": resume_percent,
        "practice": practice_percent,
        "interview": interview_percent,
    }
    lowest = sorted(scores.items(), key=lambda kv: kv[1])[:3]
    top_focus_areas = [_FOCUS_AREA_TEMPLATES[key].format(role=role_display_name) for key, _ in lowest]
    return JobReadinessRead(
        overall_percent=overall,
        skills_percent=skills_percent,
        projects_percent=projects_percent,
        resume_percent=resume_percent,
        practice_percent=practice_percent,
        interview_percent=interview_percent,
        top_focus_areas=top_focus_areas,
        is_estimate=is_estimate,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _to_read(db: Session, path: CareerPath) -> CareerPathRead:
    phases_db = (
        db.query(CareerPathPhase).filter(CareerPathPhase.career_path_id == path.id).order_by(CareerPathPhase.order_index).all()
    )
    items_db = (
        db.query(CareerPathSkillItem)
        .filter(CareerPathSkillItem.career_path_id == path.id)
        .order_by(CareerPathSkillItem.order_index)
        .all()
    )
    projects_db = (
        db.query(CareerPathProject)
        .filter(CareerPathProject.career_path_id == path.id)
        .order_by(CareerPathProject.order_index)
        .all()
    )

    items_by_phase: Dict[str, List[CareerPathSkillItem]] = {}
    for item in items_db:
        items_by_phase.setdefault(item.phase_key, []).append(item)

    phase_reads: List[CareerPathPhaseRead] = []
    for phase in phases_db:
        phase_items = items_by_phase.get(phase.phase_key, [])
        phase_projects = projects_db if phase.phase_key == "projects" else []
        status = rules.compute_phase_status([i.status for i in phase_items], [p.status for p in phase_projects])
        phase_reads.append(
            CareerPathPhaseRead(
                phase_key=phase.phase_key,
                title=phase.title,
                objective=phase.objective,
                order_index=phase.order_index,
                status=status,
                skill_items=[
                    SkillItemRead(
                        id=i.id,
                        phase_key=i.phase_key,
                        skill_key=i.skill_key,
                        title=i.title,
                        level=i.level,
                        why_it_matters=i.why_it_matters,
                        order_index=i.order_index,
                        status=i.status,
                        completed_via=i.completed_via,
                        has_practice=i.practice_blueprint_id is not None,
                    )
                    for i in phase_items
                ],
                projects=[
                    CareerPathProjectRead(
                        id=p.id,
                        project_key=p.project_key,
                        title=p.title,
                        difficulty=p.difficulty,
                        skills_demonstrated=p.skills_demonstrated,
                        technologies=p.technologies,
                        problem_statement=p.problem_statement,
                        expected_outcome=p.expected_outcome,
                        what_should_be_included=p.what_should_be_included,
                        why_recruiters_value=p.why_recruiters_value,
                        resume_bullet_suggestion=p.resume_bullet_suggestion,
                        interview_questions=p.interview_questions,
                        order_index=p.order_index,
                        status=p.status,
                    )
                    for p in phase_projects
                ],
            )
        )

    skills_completed = sum(1 for i in items_db if i.status == "completed")
    skills_total = len(items_db)
    projects_completed = sum(1 for p in projects_db if p.status == "completed")
    projects_total = len(projects_db)
    skills_percent = round(100 * skills_completed / skills_total) if skills_total else 0
    projects_percent = round(100 * projects_completed / projects_total) if projects_total else 0

    overview = path.role_overview or {}
    core_skills = list(overview.get("core_skills", []))
    tools = list(overview.get("tools", []))

    skill_gaps = _compute_skill_gaps(db, path.profile_id, core_skills, tools)
    resume_connection = _compute_resume_connection(db, path.profile_id, core_skills, projects_total)
    interview_prep = _compute_interview_prep_summary(db, path.profile_id)
    practice_percent = _compute_practice_percent(db, path.id)

    resume_percent = resume_connection.role_keyword_coverage_percent if resume_connection else 0
    interview_scores = [s.accuracy_percent for s in interview_prep if s.accuracy_percent is not None]
    interview_percent = round(sum(interview_scores) / len(interview_scores)) if interview_scores else 0

    job_readiness = _compute_job_readiness(
        path.display_role_name,
        skills_percent,
        projects_percent,
        resume_percent,
        practice_percent,
        interview_percent,
        is_estimate=(path.generated_via != "catalog"),
    )

    return CareerPathRead(
        id=path.id,
        role_id=path.role_id,
        custom_role=path.custom_role,
        role_family_key=path.role_family_key,
        display_role_name=path.display_role_name,
        is_primary=path.is_primary,
        generated_via=path.generated_via,
        role_overview=RoleOverviewRead(**overview)
        if overview
        else RoleOverviewRead(
            display_name=path.display_role_name,
            category="Other",
            short_explanation="",
            responsibilities=[],
            core_skills=[],
            tools=[],
            difficulty="moderate",
            entry_level_expectations="",
            interview_focus=[],
            portfolio_expectations="",
        ),
        phases=phase_reads,
        skill_gaps=skill_gaps,
        resume_connection=resume_connection,
        interview_prep=interview_prep,
        job_readiness=job_readiness,
        created_at=path.created_at,
        updated_at=path.updated_at,
    )


def _to_summary_read(db: Session, path: CareerPath) -> CareerPathSummaryRead:
    items_db = db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == path.id).all()
    projects_db = db.query(CareerPathProject).filter(CareerPathProject.career_path_id == path.id).all()
    completed, total = rules.compute_progress([i.status for i in items_db], [p.status for p in projects_db])
    readiness_percent = round(100 * completed / total) if total else 0
    return CareerPathSummaryRead(
        id=path.id,
        role_family_key=path.role_family_key,
        display_role_name=path.display_role_name,
        is_primary=path.is_primary,
        readiness_percent=readiness_percent,
        completed_items=completed,
        total_items=total,
        generated_via=path.generated_via,
        created_at=path.created_at,
        updated_at=path.updated_at,
    )


def get_current(db: Session, profile_id: int) -> Optional[CareerPathRead]:
    path = db.query(CareerPath).filter(CareerPath.profile_id == profile_id, CareerPath.is_primary.is_(True)).first()
    return _to_read(db, path) if path is not None else None


def list_all(db: Session, profile_id: int) -> List[CareerPathSummaryRead]:
    paths = (
        db.query(CareerPath)
        .filter(CareerPath.profile_id == profile_id)
        .order_by(CareerPath.updated_at.desc())
        .limit(_MAX_SAVED_PATHS)
        .all()
    )
    return [_to_summary_read(db, p) for p in paths]


def get_path(db: Session, profile_id: int, path_id: int) -> CareerPathRead:
    path = _get_owned_path(db, profile_id, path_id)
    return _to_read(db, path)


def set_current(db: Session, profile_id: int, path_id: int) -> CareerPathRead:
    path = _get_owned_path(db, profile_id, path_id)
    db.query(CareerPath).filter(CareerPath.profile_id == profile_id, CareerPath.id != path.id).update({"is_primary": False})
    path.is_primary = True
    db.commit()
    db.refresh(path)
    return _to_read(db, path)


# ---------------------------------------------------------------------------
# Skill item / project mutations
# ---------------------------------------------------------------------------


def get_or_create_practice_blueprint(db: Session, profile_id: int, skill_item_id: int) -> int:
    """Own single-skill AssessmentBlueprint builder -- mirrors the *pattern* used by
    placement_path_service.get_or_create_practice_blueprint, but is independent code, never
    a call into placement_path_service (Career Path must not depend on Roadmap's code)."""
    item, _path = _get_owned_skill_item(db, profile_id, skill_item_id)
    if item.practice_blueprint_id is not None:
        return item.practice_blueprint_id

    weaknesses = compute_weaknesses(db, profile_id, jd_id=None)
    weak_tags = [w.skill_tag.lower() for w in weaknesses]
    is_gap = any(skill_present(item.title, [tag]) for tag in weak_tags)

    section = BlueprintSection(
        skill=item.title,
        assessment_type=_GENERIC_ASSESSMENT_TYPE,
        weight=1.0,
        question_count=PRACTICE_QUESTION_COUNT,
        is_gap=is_gap,
    )
    blueprint = AssessmentBlueprint(
        role_id=None,
        jd_id=None,
        profile_id=profile_id,
        source=_BLUEPRINT_SOURCE,
        sections=[section.model_dump()],
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)

    item.practice_blueprint_id = blueprint.id
    if item.status == "not_started":
        item.status = "in_progress"
    db.commit()
    return blueprint.id


def mark_skill_item_status(db: Session, profile_id: int, skill_item_id: int, status: str) -> SkillItemRead:
    item, path = _get_owned_skill_item(db, profile_id, skill_item_id)
    item.status = status
    if status == "completed" and item.completed_via is None:
        item.completed_via = "manual"
    path.updated_at = _now()
    db.commit()
    db.refresh(item)
    return SkillItemRead(
        id=item.id,
        phase_key=item.phase_key,
        skill_key=item.skill_key,
        title=item.title,
        level=item.level,
        why_it_matters=item.why_it_matters,
        order_index=item.order_index,
        status=item.status,
        completed_via=item.completed_via,
        has_practice=item.practice_blueprint_id is not None,
    )


def mark_project_status(db: Session, profile_id: int, project_id: int, status: str) -> CareerPathProjectRead:
    project, path = _get_owned_project(db, profile_id, project_id)
    project.status = status
    path.updated_at = _now()
    db.commit()
    db.refresh(project)
    return CareerPathProjectRead(
        id=project.id,
        project_key=project.project_key,
        title=project.title,
        difficulty=project.difficulty,
        skills_demonstrated=project.skills_demonstrated,
        technologies=project.technologies,
        problem_statement=project.problem_statement,
        expected_outcome=project.expected_outcome,
        what_should_be_included=project.what_should_be_included,
        why_recruiters_value=project.why_recruiters_value,
        resume_bullet_suggestion=project.resume_bullet_suggestion,
        interview_questions=project.interview_questions,
        order_index=project.order_index,
        status=project.status,
    )


# ---------------------------------------------------------------------------
# JD integration
# ---------------------------------------------------------------------------


def apply_jd(db: Session, profile_id: int, path_id: int, jd_id: int) -> CareerPathRead:
    """ "Update my Career Path using this JD" -- merges a JD's required/preferred skill and
    tool requirements into the path's "core_skills" phase as new skill items (skills already
    represented among the path's existing skill items are left alone, never duplicated).
    Never touches JobPreparationPlan or any placement_path table."""
    path = _get_owned_path(db, profile_id, path_id)
    jd = db.query(JobDescription).filter(JobDescription.id == jd_id).first()
    if jd is None or jd.profile_id != profile_id:
        raise JobDescriptionNotFoundError(f"Job description {jd_id} not found")

    requirements = db.query(Requirement).filter(Requirement.jd_id == jd_id, Requirement.category.in_(["skill", "tool"])).all()

    existing_items = db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == path.id).all()
    existing_titles = [i.title for i in existing_items]
    next_order = max((i.order_index for i in existing_items if i.phase_key == "core_skills"), default=-1) + 1

    added = 0
    for req in requirements:
        if skill_present(req.value, existing_titles):
            continue  # already represented -- never duplicated
        skill_key = f"jd_{jd_id}_{req.id}"
        db.add(
            CareerPathSkillItem(
                career_path_id=path.id,
                phase_key="core_skills",
                skill_key=skill_key,
                title=req.value,
                level="intermediate",
                why_it_matters=f"Required by a job description you're preparing for ({jd.role_title or 'this role'}).",
                order_index=next_order,
                status="not_started",
            )
        )
        existing_titles.append(req.value)
        next_order += 1
        added += 1

    if added:
        path.updated_at = _now()
        db.commit()
        db.refresh(path)
    return _to_read(db, path)
