from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.jd import AIParsedJD
from app.ai.schemas.job_preparation_plan import GeneratedJobPreparationContent
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.interview import Interview
from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.job_preparation_plan import JobPreparationPlan
from app.models.project import Project
from app.models.requirement import Requirement
from app.models.role import Role
from app.models.skill_gap import SkillGap
from app.schemas.job import (
    InterviewPrepQuestionRead,
    JobDescriptionListItem,
    JobDescriptionParseResponse,
    JobMatchRead,
    JobPreparationPlanRead,
    JobSummaryRead,
    PreparationPlanCategoryRead,
    RequiredSkillRead,
    RequirementRead,
    SkillGapRead,
    SkillGapStatusRead,
)
from app.services import placement_path_rules as rules
from app.services.matching_service import match_resume_to_job
from app.services.weakness_service import compute_weaknesses


class JobDescriptionInUseError(Exception):
    """Raised when trying to delete a JD that an AssessmentBlueprint or Interview still
    references -- deleting it would leave those with a dangling jd_id."""

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "jd_parser_v1.txt"
_PREP_PLAN_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "ai" / "prompts" / "jd_preparation_plan_v1.txt"
)

# Always-relevant preparation categories for this app's campus-placement domain -- matches
# placement_path_rules.py's own "DSA/Aptitude are baseline, near-universal" philosophy.
_ALWAYS_RELEVANT_CATEGORIES = ["DSA", "Aptitude", "Programming", "Core CS", "Technical", "HR", "Resume"]
# Domain tracks (from placement_path_rules) whose presence in the JD's own required/preferred
# skills makes "System Design" a relevant prep category -- roles with a real backend/cloud/ML
# stack are the ones where architecture questions actually come up.
_SYSTEM_DESIGN_TRIGGER_TRACKS = {"web_backend", "cloud_devops", "ml_data_science"}


def _build_prompt(jd_text: str) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(jd_text=jd_text)


def detect_role(db: Session, role_title: str) -> Optional[Role]:
    """Best-effort case-insensitive substring match against the seeded Role catalog.
    The AI has no knowledge of our internal role ids, so this is resolved here rather
    than trusting a model-generated id. A future stage can replace this heuristic with
    something smarter (embeddings, an AI-assisted match) without changing the contract."""
    title_lower = role_title.strip().lower()
    if not title_lower:
        # An empty role_title would make `title_lower in name_lower` trivially True for
        # every role, silently detecting whichever role happens to be iterated first.
        return None
    for role in db.query(Role).all():
        name_lower = role.name.lower()
        if name_lower in title_lower or title_lower in name_lower:
            return role
    return None


def _requirement_read(requirement: Requirement) -> RequirementRead:
    return RequirementRead(
        id=requirement.id,
        category=requirement.category,
        value=requirement.value,
        is_explicit=requirement.is_explicit,
        importance=requirement.importance,
    )


def parse_job_description(
    db: Session, profile_id: int, raw_text: str, ai_provider: AIProvider
) -> JobDescriptionParseResponse:
    parsed: AIParsedJD = ai_provider.generate_structured(_build_prompt(raw_text), AIParsedJD)

    detected_role = detect_role(db, parsed.role_title)

    jd = JobDescription(
        profile_id=profile_id,
        raw_text=raw_text,
        source="paste",
        detected_role_id=detected_role.id if detected_role else None,
        role_title=parsed.role_title,
        location=parsed.location,
        employment_type=parsed.employment_type,
        parsing_confidence=parsed.parsing_confidence,
    )
    db.add(jd)
    db.commit()
    db.refresh(jd)

    requirements: list[Requirement] = []
    for item in parsed.required_skills:
        requirements.append(
            Requirement(
                jd_id=jd.id, category="skill", value=item.value,
                is_explicit=item.is_explicit, importance="required",
            )
        )
    for item in parsed.preferred_skills:
        requirements.append(
            Requirement(
                jd_id=jd.id, category="skill", value=item.value,
                is_explicit=item.is_explicit, importance="preferred",
            )
        )
    for tool in parsed.tools_technologies:
        requirements.append(
            Requirement(jd_id=jd.id, category="tool", value=tool, is_explicit=True, importance="required")
        )
    for responsibility in parsed.responsibilities:
        requirements.append(
            Requirement(
                jd_id=jd.id, category="responsibility", value=responsibility,
                is_explicit=True, importance="required",
            )
        )
    if parsed.experience_expectation:
        requirements.append(
            Requirement(
                jd_id=jd.id, category="experience", value=parsed.experience_expectation,
                is_explicit=True, importance="required",
            )
        )
    if parsed.education_eligibility:
        requirements.append(
            Requirement(
                jd_id=jd.id, category="education", value=parsed.education_eligibility,
                is_explicit=True, importance="required",
            )
        )

    db.add_all(requirements)
    db.commit()
    for requirement in requirements:
        db.refresh(requirement)

    return JobDescriptionParseResponse(
        id=jd.id,
        role_title=jd.role_title,
        detected_role_id=jd.detected_role_id,
        location=jd.location,
        employment_type=jd.employment_type,
        parsing_confidence=jd.parsing_confidence,
        requirements=[_requirement_read(r) for r in requirements],
    )


def get_requirements(db: Session, jd_id: int, profile_id: int) -> Optional[List[RequirementRead]]:
    jd = db.get(JobDescription, jd_id)
    if jd is None or jd.profile_id != profile_id:
        return None

    requirements = db.query(Requirement).filter(Requirement.jd_id == jd_id).all()
    return [_requirement_read(r) for r in requirements]


def list_job_descriptions(db: Session, profile_id: int) -> List[JobDescriptionListItem]:
    """Past JD analyses for this profile, most recent first -- backs the Android app's
    Job Description Analyzer 'past analyses' list (save/reopen/delete)."""
    jds = (
        db.query(JobDescription)
        .filter(JobDescription.profile_id == profile_id)
        .order_by(JobDescription.created_at.desc())
        .all()
    )
    jd_ids = [jd.id for jd in jds]
    latest_plan_by_jd: Dict[int, JobPreparationPlan] = {}
    if jd_ids:
        # Ordered (jd_id, id desc) so the first row seen per jd_id in this single pass is
        # each JD's latest plan -- avoids one query per JD for what's otherwise a "latest
        # row per group" lookup.
        plans = (
            db.query(JobPreparationPlan)
            .filter(JobPreparationPlan.jd_id.in_(jd_ids), JobPreparationPlan.profile_id == profile_id)
            .order_by(JobPreparationPlan.jd_id, JobPreparationPlan.id.desc())
            .all()
        )
        for plan in plans:
            latest_plan_by_jd.setdefault(plan.jd_id, plan)

    items: List[JobDescriptionListItem] = []
    for jd in jds:
        plan = latest_plan_by_jd.get(jd.id)
        items.append(
            JobDescriptionListItem(
                id=jd.id,
                role_title=jd.role_title or "Untitled role",
                location=jd.location,
                created_at=jd.created_at,
                has_preparation_plan=plan is not None,
                preparation_plan_company_name=plan.company_name if plan else None,
                preparation_plan_job_title=plan.job_title if plan else None,
            )
        )
    return items


def delete_job_description(db: Session, jd_id: int, profile_id: int) -> Optional[bool]:
    """PROJECT_HANDOFF.md section 6: "Provide deletion controls for user data" -- returns
    None if not found/not owned, raises JobDescriptionInUseError if a blueprint or
    interview already depends on this JD (deleting it would dangle their jd_id), True on
    success."""
    jd = db.get(JobDescription, jd_id)
    if jd is None or jd.profile_id != profile_id:
        return None

    in_use = (
        db.query(AssessmentBlueprint).filter(AssessmentBlueprint.jd_id == jd_id).first() is not None
        or db.query(Interview).filter(Interview.jd_id == jd_id).first() is not None
    )
    if in_use:
        raise JobDescriptionInUseError(
            "This job description has already been used to generate a preparation plan or "
            "interview and can't be deleted"
        )

    job_match_ids = [m.id for m in db.query(JobMatch.id).filter(JobMatch.jd_id == jd_id).all()]
    if job_match_ids:
        db.query(SkillGap).filter(SkillGap.job_match_id.in_(job_match_ids)).delete(synchronize_session=False)
    db.query(JobMatch).filter(JobMatch.jd_id == jd_id).delete(synchronize_session=False)
    db.query(JobPreparationPlan).filter(JobPreparationPlan.jd_id == jd_id).delete(synchronize_session=False)
    db.query(Requirement).filter(Requirement.jd_id == jd_id).delete(synchronize_session=False)
    db.delete(jd)
    db.commit()
    return True


# --- Preparation plan: composes already-parsed JD + already-computed gaps, one AI call ----


def _job_match_read(db: Session, job_match: JobMatch) -> JobMatchRead:
    gaps = db.query(SkillGap).filter(SkillGap.job_match_id == job_match.id).all()
    return JobMatchRead(
        id=job_match.id,
        matching_skills=job_match.matching_skills,
        missing_skills=job_match.missing_skills,
        match_score=job_match.match_score,
        skill_gaps=[
            SkillGapRead(id=g.id, skill_name=g.skill_name, gap_severity=g.gap_severity, source=g.source)
            for g in gaps
        ],
    )


def _ensure_job_match(db: Session, jd_id: int, profile_id: int) -> JobMatchRead:
    """Reuses an existing match if one was already computed (e.g. via the Analyzer's own
    match step) instead of recomputing -- match_resume_to_job always inserts a fresh row,
    so calling it unconditionally here would leave duplicate JobMatch rows behind."""
    existing = (
        db.query(JobMatch)
        .filter(JobMatch.jd_id == jd_id, JobMatch.profile_id == profile_id)
        .order_by(JobMatch.id.desc())
        .first()
    )
    if existing is not None:
        return _job_match_read(db, existing)
    result = match_resume_to_job(db, jd_id, profile_id)  # propagates NoResumeAnalysisError
    assert result is not None  # jd existence already verified by the caller
    return result


def _relevant_categories(db: Session, jd_id: int, profile_id: int) -> List[str]:
    """Rule-based, reusing placement_path_rules.match_domain_tracks (a pure function of a
    skill-name list -- works identically whether fed resume skills or, as here, JD required/
    preferred skill values) rather than a new AI classification call."""
    jd_skill_values = [
        r.value
        for r in db.query(Requirement).filter(Requirement.jd_id == jd_id, Requirement.category == "skill").all()
    ]
    matched_tracks = rules.match_domain_tracks(jd_skill_values)

    categories = list(_ALWAYS_RELEVANT_CATEGORIES)
    if "sql" in matched_tracks:
        categories.append("SQL")
    if matched_tracks.keys() & _SYSTEM_DESIGN_TRIGGER_TRACKS:
        categories.append("System Design")
    has_projects = db.query(Project).filter(Project.profile_id == profile_id).first() is not None
    if has_projects:
        categories.append("Projects")
    return categories


def _fallback_category_reason(category: str, role_title: str) -> str:
    return f"{category} is part of a well-rounded preparation plan for the {role_title} role."


def _bucket_skill_gaps(job_match: JobMatchRead, weaknesses_by_tag: dict) -> List[SkillGapStatusRead]:
    gaps: List[SkillGapStatusRead] = []
    for skill in job_match.matching_skills:
        is_weak = skill.lower() in weaknesses_by_tag
        gaps.append(SkillGapStatusRead(skill=skill, status="needs_improvement" if is_weak else "strong"))
    for skill in job_match.missing_skills:
        gaps.append(SkillGapStatusRead(skill=skill, status="missing"))
    return gaps


def _build_prep_plan_prompt(role_title: str, company_name: Optional[str], job_match: JobMatchRead, categories: List[str]) -> str:
    template = _PREP_PLAN_PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(
        role_title=role_title,
        company_name=company_name or "not specified",
        matching_skills=", ".join(job_match.matching_skills) or "none identified",
        missing_skills=", ".join(job_match.missing_skills) or "none identified",
        category_names="\n".join(f"- {c}" for c in categories),
    )


def generate_preparation_plan(
    db: Session,
    profile_id: int,
    jd_id: int,
    ai_provider: AIProvider,
    company_name: Optional[str],
    job_title: Optional[str],
) -> Optional[JobPreparationPlanRead]:
    jd = db.get(JobDescription, jd_id)
    if jd is None or jd.profile_id != profile_id:
        return None

    job_match = _ensure_job_match(db, jd_id, profile_id)  # propagates NoResumeAnalysisError

    requirements = get_requirements(db, jd_id, profile_id) or []
    required_skills = [
        RequiredSkillRead(category=r.category, value=r.value, importance=r.importance)
        for r in requirements
        if r.category in ("skill", "tool")
    ]

    weaknesses = compute_weaknesses(db, profile_id, jd_id)
    weaknesses_by_tag = {w.skill_tag.lower(): w.severity for w in weaknesses}
    preparation_gaps = _bucket_skill_gaps(job_match, weaknesses_by_tag)

    categories = _relevant_categories(db, jd_id, profile_id)
    role_title = job_title or jd.role_title or "this role"

    category_reasons: dict = {}
    interview_questions: List[InterviewPrepQuestionRead] = []
    try:
        prompt = _build_prep_plan_prompt(role_title, company_name, job_match, categories)
        generated: GeneratedJobPreparationContent = ai_provider.generate_structured(
            prompt, GeneratedJobPreparationContent
        )
        for item in generated.category_reasons:
            if item.category in categories:
                category_reasons[item.category] = item.reason
        interview_questions = [
            InterviewPrepQuestionRead(bucket=q.bucket, question=q.question) for q in generated.interview_questions
        ]
    except AIGenerationError:
        pass  # never blocks plan generation -- categories fall back to a template reason below

    preparation_plan = [
        PreparationPlanCategoryRead(
            category=c, reason=category_reasons.get(c) or _fallback_category_reason(c, role_title)
        )
        for c in categories
    ]

    job_summary = JobSummaryRead(
        company_name=company_name,
        job_title=role_title,
        experience_expectation=next((r.value for r in requirements if r.category == "experience"), None),
        education_eligibility=next((r.value for r in requirements if r.category == "education"), None),
        location=jd.location,
        employment_type=jd.employment_type,
    )

    plan = JobPreparationPlan(
        jd_id=jd_id,
        profile_id=profile_id,
        company_name=company_name,
        job_title=job_title,
        job_summary=job_summary.model_dump(),
        required_skills=[s.model_dump() for s in required_skills],
        preparation_gaps=[g.model_dump() for g in preparation_gaps],
        preparation_plan=[p.model_dump() for p in preparation_plan],
        interview_prep=[q.model_dump() for q in interview_questions],
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    return JobPreparationPlanRead(
        id=plan.id,
        jd_id=plan.jd_id,
        company_name=plan.company_name,
        job_title=plan.job_title,
        job_summary=job_summary,
        required_skills=required_skills,
        preparation_gaps=preparation_gaps,
        preparation_plan=preparation_plan,
        interview_prep=interview_questions,
        created_at=plan.created_at,
    )


def get_preparation_plan(db: Session, jd_id: int, profile_id: int) -> Optional[JobPreparationPlanRead]:
    jd = db.get(JobDescription, jd_id)
    if jd is None or jd.profile_id != profile_id:
        return None
    plan = (
        db.query(JobPreparationPlan)
        .filter(JobPreparationPlan.jd_id == jd_id, JobPreparationPlan.profile_id == profile_id)
        .order_by(JobPreparationPlan.id.desc())
        .first()
    )
    if plan is None:
        return None
    return JobPreparationPlanRead(
        id=plan.id,
        jd_id=plan.jd_id,
        company_name=plan.company_name,
        job_title=plan.job_title,
        job_summary=JobSummaryRead(**plan.job_summary),
        required_skills=[RequiredSkillRead(**s) for s in plan.required_skills],
        preparation_gaps=[SkillGapStatusRead(**g) for g in plan.preparation_gaps],
        preparation_plan=[PreparationPlanCategoryRead(**p) for p in plan.preparation_plan],
        interview_prep=[InterviewPrepQuestionRead(**q) for q in plan.interview_prep],
        created_at=plan.created_at,
    )
