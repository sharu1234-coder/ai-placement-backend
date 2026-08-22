from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.ai.provider import AIProvider
from app.ai.schemas.jd import AIParsedJD
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.interview import Interview
from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.requirement import Requirement
from app.models.role import Role
from app.models.skill_gap import SkillGap
from app.schemas.job import JobDescriptionParseResponse, RequirementRead


class JobDescriptionInUseError(Exception):
    """Raised when trying to delete a JD that an AssessmentBlueprint or Interview still
    references -- deleting it would leave those with a dangling jd_id."""

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "jd_parser_v1.txt"


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
        parsing_confidence=jd.parsing_confidence,
        requirements=[_requirement_read(r) for r in requirements],
    )


def get_requirements(db: Session, jd_id: int, profile_id: int) -> Optional[List[RequirementRead]]:
    jd = db.get(JobDescription, jd_id)
    if jd is None or jd.profile_id != profile_id:
        return None

    requirements = db.query(Requirement).filter(Requirement.jd_id == jd_id).all()
    return [_requirement_read(r) for r in requirements]


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
    db.query(Requirement).filter(Requirement.jd_id == jd_id).delete(synchronize_session=False)
    db.delete(jd)
    db.commit()
    return True
