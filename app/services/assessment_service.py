from collections import defaultdict
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.job_description import JobDescription
from app.models.question_attempt import QuestionAttempt
from app.models.requirement import Requirement
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.schemas.assessment import AssessmentBlueprintRead, AssessmentHistoryItem, BlueprintSection
from app.services.skill_matching import NoResumeAnalysisError, latest_resume_skills, skill_present

# Safety cap, not a product pagination feature (disclosed gap -- true cursor/page-based
# pagination would need a coordinated backend+Android API contract change, out of scope for
# a same-shape defensive fix): protects response size/memory for a long-lived account with
# an unusually large history, without changing this endpoint's plain-list response shape.
_MAX_HISTORY_ROWS = 200

_JD_REQUIRED_WEIGHT = 0.9
_JD_PREFERRED_WEIGHT = 0.6


class InvalidPrepareMeRequestError(Exception):
    """Raised when role_id/jd_id don't resolve to anything we can build a blueprint from."""


def _question_count(weight: float, is_gap: bool) -> int:
    count = 2 if weight < 0.5 else 3 if weight < 0.8 else 5
    if is_gap:
        count = min(count + 1, 6)
    return count


def _build_skill_matrix(
    db: Session, role_id: Optional[int], jd_id: Optional[int]
) -> tuple[dict, str]:
    """Returns (skill_lower -> {skill, weight, assessment_type}), source label."""
    matrix: dict = {}
    source = "default_taxonomy"

    if role_id is not None:
        for row in db.query(RoleSkillWeight).filter(RoleSkillWeight.role_id == role_id).all():
            matrix[row.skill.lower()] = {
                "skill": row.skill,
                "weight": row.weight,
                "assessment_type": row.assessment_type,
            }

    if jd_id is not None:
        jd_skills = (
            db.query(Requirement)
            .filter(Requirement.jd_id == jd_id, Requirement.category == "skill")
            .all()
        )
        if jd_skills:
            source = "jd_override"
            for req in jd_skills:
                key = req.value.lower()
                jd_weight = _JD_REQUIRED_WEIGHT if req.importance == "required" else _JD_PREFERRED_WEIGHT
                if key in matrix:
                    matrix[key]["weight"] = max(matrix[key]["weight"], jd_weight)
                else:
                    matrix[key] = {
                        "skill": req.value,
                        "weight": jd_weight,
                        "assessment_type": "technical",
                    }

    return matrix, source


def read_blueprint(
    db: Session, profile_id: int, blueprint_id: int
) -> Optional[AssessmentBlueprintRead]:
    """This student's blueprint as generate_blueprint would have returned it, or None.

    Answers a replayed create from the blueprint the first attempt built, so a lost response
    does not leave an orphaned one behind. Sections are rebuilt from the stored JSON, which is
    the same list the original response carried. Ownership is re-checked here.
    """
    blueprint = db.get(AssessmentBlueprint, blueprint_id)
    if blueprint is None or blueprint.profile_id != profile_id:
        return None
    return AssessmentBlueprintRead(
        id=blueprint.id,
        role_id=blueprint.role_id,
        jd_id=blueprint.jd_id,
        source=blueprint.source,
        sections=[BlueprintSection(**s) for s in blueprint.sections],
    )


def generate_blueprint(
    db: Session,
    profile_id: int,
    role_id: Optional[int],
    jd_id: Optional[int],
    category: Optional[str] = None,
) -> AssessmentBlueprintRead:
    effective_role_id = role_id

    if jd_id is not None:
        jd = db.get(JobDescription, jd_id)
        if jd is None or jd.profile_id != profile_id:
            raise InvalidPrepareMeRequestError("Job description not found")
        if effective_role_id is None:
            effective_role_id = jd.detected_role_id

    if effective_role_id is not None and db.get(Role, effective_role_id) is None:
        raise InvalidPrepareMeRequestError("Role not found")

    matrix, source = _build_skill_matrix(db, effective_role_id, jd_id)
    if category is not None:
        matrix = {k: v for k, v in matrix.items() if v["assessment_type"] == category}
        if not matrix:
            raise InvalidPrepareMeRequestError(f"No {category} skills found for this role/JD")
    if not matrix:
        raise InvalidPrepareMeRequestError(
            "Not enough information to build a blueprint -- select a role or provide a JD "
            "with recognizable skills"
        )

    try:
        resume_skills = latest_resume_skills(db, profile_id)
    except NoResumeAnalysisError:
        resume_skills = []

    sections: list[BlueprintSection] = []
    for entry in matrix.values():
        is_gap = not skill_present(entry["skill"], resume_skills) if resume_skills else True
        sections.append(
            BlueprintSection(
                skill=entry["skill"],
                assessment_type=entry["assessment_type"],
                weight=entry["weight"],
                question_count=_question_count(entry["weight"], is_gap),
                is_gap=is_gap,
            )
        )

    sections.sort(key=lambda s: s.weight * (1.5 if s.is_gap else 1.0), reverse=True)

    blueprint = AssessmentBlueprint(
        role_id=effective_role_id,
        jd_id=jd_id,
        profile_id=profile_id,
        source=source,
        sections=[s.model_dump() for s in sections],
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)

    return AssessmentBlueprintRead(
        id=blueprint.id,
        role_id=blueprint.role_id,
        jd_id=blueprint.jd_id,
        source=blueprint.source,
        sections=sections,
    )


def get_assessment_history(db: Session, profile_id: int) -> List[AssessmentHistoryItem]:
    """Completed tests only (an AssessmentResult row exists only once a test is finished --
    see assessment_run_service.get_result), most recent first. Powers the Test Center's
    stats (client-side averages over this list) and recent-results display."""
    rows = (
        db.query(Assessment, AssessmentBlueprint, AssessmentResult)
        .join(AssessmentBlueprint, AssessmentBlueprint.id == Assessment.blueprint_id)
        .join(AssessmentResult, AssessmentResult.assessment_id == Assessment.id)
        .filter(Assessment.profile_id == profile_id)
        .order_by(AssessmentResult.created_at.desc())
        .limit(_MAX_HISTORY_ROWS)
        .all()
    )

    assessment_ids = [assessment.id for assessment, _, _ in rows]
    attempts_by_assessment: Dict[int, List[QuestionAttempt]] = defaultdict(list)
    if assessment_ids:
        all_attempts = (
            db.query(QuestionAttempt)
            .filter(QuestionAttempt.assessment_id.in_(assessment_ids), QuestionAttempt.is_correct.isnot(None))
            .all()
        )
        for attempt in all_attempts:
            attempts_by_assessment[attempt.assessment_id].append(attempt)

    items: List[AssessmentHistoryItem] = []
    for assessment, blueprint, result in rows:
        sections = blueprint.sections or []
        dominant_category = (
            max(sections, key=lambda s: s.get("weight", 0.0))["assessment_type"] if sections else "technical"
        )
        attempts = attempts_by_assessment[assessment.id]
        accuracy = (sum(1 for a in attempts if a.is_correct) / len(attempts)) if attempts else 0.0
        items.append(
            AssessmentHistoryItem(
                assessment_id=assessment.id,
                blueprint_id=blueprint.id,
                category=dominant_category,
                overall_score=result.overall_score,
                accuracy=round(accuracy, 3),
                question_count=len(attempts),
                completed_at=result.created_at,
            )
        )
    return items
