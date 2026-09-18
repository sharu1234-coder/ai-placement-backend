import logging
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
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
    GapGuidanceRead,
    InterviewPrepQuestionRead,
    JobDescriptionListItem,
    JobDescriptionParseResponse,
    JobMatchRead,
    JobPreparationPlanRead,
    JobSummaryRead,
    PreparationPlanCategoryRead,
    ProjectConnectionRead,
    RequiredSkillRead,
    RequirementRead,
    ResumeRecommendationItemRead,
    ResumeRecommendationsRead,
    SkillGapRead,
    SkillGapStatusRead,
)
from app.services import jd_grounding
from app.services import placement_path_rules as rules
from app.services.matching_service import match_resume_to_job
from app.services.skill_matching import NoResumeAnalysisError
from app.services.weakness_service import compute_weaknesses

logger = logging.getLogger(__name__)


class JobDescriptionInUseError(Exception):
    """Raised when trying to delete a JD that an AssessmentBlueprint or Interview still
    references -- deleting it would leave those with a dangling jd_id."""

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "jd_parser_v2.txt"
_PREP_PLAN_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "ai" / "prompts" / "jd_preparation_plan_v4.txt"
)

# Safety cap, not a product pagination feature (disclosed gap -- see assessment_service.py's
# _MAX_HISTORY_ROWS for the full rationale): protects response size for a long-lived account.
_MAX_HISTORY_ROWS = 200

# Always-relevant preparation categories for this app's campus-placement domain -- matches
# placement_path_rules.py's own "DSA/Aptitude are baseline, near-universal" philosophy.
_ALWAYS_RELEVANT_CATEGORIES = ["DSA", "Aptitude", "Programming", "Core CS", "Technical", "HR", "Resume"]
# Domain tracks (from placement_path_rules) whose presence in the JD's own required/preferred
# skills makes "System Design" a relevant prep category -- roles with a real backend/cloud/ML
# stack are the ones where architecture questions actually come up.
_SYSTEM_DESIGN_TRIGGER_TRACKS = {"web_backend", "cloud_devops", "ml_data_science"}


def _build_prompt(jd_text: str) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return render_prompt(template, jd_text=jd_text)


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
    db: Session, profile_id: int, raw_text: str, ai_provider: AIProvider, source: str = "paste"
) -> JobDescriptionParseResponse:
    """Parse a job description into a JobDescription plus its Requirement rows.

    `source` records how the text arrived ("paste" or "pdf_upload") and defaults to the
    original value so existing callers are unaffected. It changes nothing about parsing: a JD
    extracted from a PDF is treated as exactly the same untrusted text as a pasted one.
    """
    parsed: AIParsedJD = ai_provider.generate_structured(_build_prompt(raw_text), AIParsedJD)

    detected_role = detect_role(db, parsed.role_title)

    jd = JobDescription(
        profile_id=profile_id,
        raw_text=raw_text,
        source=source,
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
        .limit(_MAX_HISTORY_ROWS)
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
        # Same caveat as career_path/service._compute_skill_gaps: "strong" here means the JD's
        # skill matched the student's resume and nothing flagged it weak -- a claim, not
        # evidence. It is a different, weaker thing than evidence_engine's "strong", and the
        # client labels it "On Your Resume" so the two are not confused on screen.
        gaps.append(SkillGapStatusRead(skill=skill, status="needs_improvement" if is_weak else "strong"))
    for skill in job_match.missing_skills:
        gaps.append(SkillGapStatusRead(skill=skill, status="missing"))
    return gaps


def _build_prep_plan_prompt(
    role_title: str,
    company_name: Optional[str],
    matching_skills: List[str],
    missing_skills: List[str],
    categories: List[str],
    gaps: List[jd_grounding.GroundedGap],
    facts: jd_grounding.ResumeFacts,
    project_matches: List[jd_grounding.GroundedProjectMatch],
) -> str:
    """One prompt, carrying every grounded block the single AI call needs.

    Takes the skill lists directly rather than a JobMatchRead so that the no-resume path --
    which has no JobMatch at all -- can render the same prompt with empty lists, instead of
    needing a fabricated match object just to satisfy a type.
    """
    template = _PREP_PLAN_PROMPT_PATH.read_text(encoding="utf-8")
    return render_prompt(
        template,
        role_title=role_title,
        company_name=company_name or "not specified",
        matching_skills=", ".join(matching_skills) or "none identified",
        missing_skills=", ".join(missing_skills) or "none identified",
        category_names="\n".join(f"- {c}" for c in categories),
        gap_context=jd_grounding.render_gap_context(gaps),
        resume_facts=jd_grounding.render_resume_facts(facts),
        project_context=jd_grounding.render_project_context(project_matches),
    )


def _assemble_gap_guidance(
    gaps: List[jd_grounding.GroundedGap], generated: Optional[GeneratedJobPreparationContent]
) -> List[GapGuidanceRead]:
    """Deterministic gap facts + generated prose, joined on the skill name.

    The loop runs over the grounded gaps, never over what the AI returned: a gap the model
    ignored still reaches the student (with a template sentence), and a "gap" the model
    invented has no grounded entry to attach to, so it is dropped.
    """
    by_skill = {}
    if generated is not None:
        by_skill = {g.skill.lower(): g for g in generated.gap_guidance}

    guidance: List[GapGuidanceRead] = []
    for gap in gaps:
        written = by_skill.get(gap.skill.lower())
        framing = (
            "The job description asks for this directly."
            if gap.is_explicit_requirement
            else "The job description doesn't name this outright -- we inferred it from the role."
        )
        guidance.append(
            GapGuidanceRead(
                skill=gap.skill,
                severity=gap.severity,
                is_explicit_requirement=gap.is_explicit_requirement,
                requirement_importance=gap.requirement_importance,
                why_it_matters=written.why_it_matters if written else framing,
                recommended_action=(
                    written.recommended_action if written else f"Start with the fundamentals of {gap.skill}."
                ),
                practice_direction=(
                    written.practice_direction
                    if written
                    else f"Build something small that uses {gap.skill} end to end."
                ),
            )
        )
    return guidance


def _assemble_resume_recommendations(
    facts: jd_grounding.ResumeFacts,
    keywords: List[str],
    gaps: List[jd_grounding.GroundedGap],
    requirement_values: List[str],
    generated: Optional[GeneratedJobPreparationContent],
) -> ResumeRecommendationsRead:
    """Recommendations, or an explicit reason why there are none.

    Never invents a status: with no resume the student is told exactly that, rather than being
    shown generic advice dressed up as tailored analysis.

    Every generated item is then put through jd_grounding.ground_resume_recommendations before
    it reaches the student. Unlike gap guidance and project connections -- which are safe
    because they iterate a deterministic list and look the model's prose up by key -- this list
    is produced by the model itself, so there is no key that an invented entry could fail to
    match. Validating each item is what gives this section the same guarantee the other two get
    for free, rather than relying on the prompt's instruction not to invent.
    """
    if not facts.has_resume:
        return ResumeRecommendationsRead(status="no_resume", missing_keywords=[], items=[])
    if not facts.is_informative:
        return ResumeRecommendationsRead(status="insufficient_resume_data", missing_keywords=keywords, items=[])

    grounded = jd_grounding.ground_resume_recommendations(
        facts, gaps, requirement_values, list(generated.resume_recommendations) if generated else []
    )
    items = [
        ResumeRecommendationItemRead(
            area=written.area,
            requirement=written.requirement,
            current_fact=written.current_fact,
            suggestion=written.suggestion,
            rationale=written.rationale,
        )
        for written in grounded
    ]
    return ResumeRecommendationsRead(status="ready", missing_keywords=keywords, items=items)


def _assemble_project_connections(
    matches: List[jd_grounding.GroundedProjectMatch], generated: Optional[GeneratedJobPreparationContent]
) -> List[ProjectConnectionRead]:
    """Only the deterministically matched (requirement, project) pairs survive.

    Keyed on both halves, so a model that pairs a real project with a requirement it was never
    matched to produces nothing -- that pairing is itself a claim about the student's
    experience, and it has to be earned by the text of their own project.
    """
    written_by_key = {}
    if generated is not None:
        written_by_key = {
            (c.project_title.lower(), c.requirement.lower()): c for c in generated.project_connections
        }

    connections: List[ProjectConnectionRead] = []
    for match in matches:
        written = written_by_key.get((match.project.title.lower(), match.requirement.lower()))
        connections.append(
            ProjectConnectionRead(
                requirement=match.requirement,
                project_title=match.project.title,
                project_source=match.project.source,
                why_relevant=(
                    written.why_relevant
                    if written
                    else f"You have hands-on work with {match.requirement} in this project."
                ),
                likely_questions=list(written.likely_questions[:4]) if written else [],
            )
        )
    return connections


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

    # A student may analyse a job before they have uploaded anything. That used to raise, which
    # meant the entire plan -- including every part that depends only on the JD -- was withheld
    # over a resume we never needed to read the posting. Matching is the only part that
    # genuinely requires one, so only matching is skipped.
    facts = jd_grounding.collect_resume_facts(db, profile_id)
    job_match: Optional[JobMatchRead] = None
    if facts.has_resume:
        try:
            job_match = _ensure_job_match(db, jd_id, profile_id)
        except NoResumeAnalysisError:
            # collect_resume_facts and the matcher read the same rows, so this is only
            # reachable if they were deleted between the two reads. Treated as "no resume"
            # rather than failing a plan that is otherwise complete.
            logger.info("Resume disappeared mid-generation for jd_id=%s; continuing without matching", jd_id)

    matching_skills = job_match.matching_skills if job_match else []
    missing_skills = job_match.missing_skills if job_match else []

    requirements = get_requirements(db, jd_id, profile_id) or []
    required_skills = [
        RequiredSkillRead(category=r.category, value=r.value, importance=r.importance)
        for r in requirements
        if r.category in ("skill", "tool")
    ]

    weaknesses = compute_weaknesses(db, profile_id, jd_id)
    weaknesses_by_tag = {w.skill_tag.lower(): w.severity for w in weaknesses}
    preparation_gaps = _bucket_skill_gaps(job_match, weaknesses_by_tag) if job_match else []

    # Everything below is established in code before the model is asked anything, so the AI can
    # only ever write prose about facts that already exist.
    matchable = jd_grounding.matchable_requirements(db, jd_id)
    gap_severities = {
        gap.skill: ("high" if gap.status == "missing" else "medium")
        for gap in preparation_gaps
        if gap.status in ("missing", "needs_improvement")
    }
    grounded_gaps = jd_grounding.ground_gaps(matchable, gap_severities)
    keywords = jd_grounding.missing_keywords(matchable, facts)
    project_matches = jd_grounding.match_projects(matchable, facts) if facts.has_resume else []

    categories = _relevant_categories(db, jd_id, profile_id)
    role_title = job_title or jd.role_title or "this role"

    category_reasons: dict = {}
    interview_questions: List[InterviewPrepQuestionRead] = []
    generated: Optional[GeneratedJobPreparationContent] = None
    try:
        prompt = _build_prep_plan_prompt(
            role_title,
            company_name,
            matching_skills,
            missing_skills,
            categories,
            grounded_gaps,
            facts,
            project_matches,
        )
        generated = ai_provider.generate_structured(prompt, GeneratedJobPreparationContent)
        for item in generated.category_reasons:
            if item.category in categories:
                category_reasons[item.category] = item.reason
        interview_questions = [
            InterviewPrepQuestionRead(bucket=q.bucket, question=q.question) for q in generated.interview_questions
        ]
    except AIGenerationError as exc:
        # Never blocks plan generation -- categories fall back to a template reason below, and
        # the three grounded sections fall back to their deterministic halves.
        logger.warning("AI prep-plan generation failed for jd_id=%s, using template fallback: %s", jd_id, exc)

    gap_guidance = _assemble_gap_guidance(grounded_gaps, generated)
    resume_recommendations = _assemble_resume_recommendations(
        facts, keywords, grounded_gaps, [r.value for r in requirements], generated
    )
    project_connections = _assemble_project_connections(project_matches, generated)

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
        gap_guidance=[g.model_dump() for g in gap_guidance],
        resume_recommendations=resume_recommendations.model_dump(),
        project_connections=[c.model_dump() for c in project_connections],
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
        gap_guidance=gap_guidance,
        resume_recommendations=resume_recommendations,
        project_connections=project_connections,
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
        # Plans saved before these columns existed read back as NULL, and stay NULL rather
        # than being back-filled with invented content -- the client shows the sections it has.
        gap_guidance=(
            [GapGuidanceRead(**g) for g in plan.gap_guidance] if plan.gap_guidance is not None else None
        ),
        resume_recommendations=(
            ResumeRecommendationsRead(**plan.resume_recommendations)
            if plan.resume_recommendations is not None
            else None
        ),
        project_connections=(
            [ProjectConnectionRead(**c) for c in plan.project_connections]
            if plan.project_connections is not None
            else None
        ),
        created_at=plan.created_at,
    )
