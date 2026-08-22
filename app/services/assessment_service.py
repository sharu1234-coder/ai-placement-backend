from typing import Optional

from sqlalchemy.orm import Session

from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.job_description import JobDescription
from app.models.requirement import Requirement
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.schemas.assessment import AssessmentBlueprintRead, BlueprintSection
from app.services.skill_matching import NoResumeAnalysisError, latest_resume_skills, skill_present

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


def generate_blueprint(
    db: Session, profile_id: int, role_id: Optional[int], jd_id: Optional[int]
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
