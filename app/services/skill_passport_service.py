"""Skill Passport -- the per-skill deep view over Evidence Engine: "Python -- Strongly
supported by evidence", with the "WHY IS THIS VERIFIED?" evidence list the product spec calls
for. Reuses role_context (the same role resolution Career Twin uses) purely to know *which*
skills belong to the profile's current role, then reads Evidence Engine directly -- Skill
Passport never calls into career_twin_service, keeping the two features independent reads over
shared generic infra, not a dependency of one on the other.
"""

from typing import List

from sqlalchemy.orm import Session

from app.models.profile import StudentProfile
from app.schemas.skill_passport import EvidenceItemRead, SkillPassportItemRead
from app.services import evidence_engine
from app.services.role_context import resolve_role_context

# A level is a coarse, honest label derived from the SAME state Evidence Engine already
# computed -- never a separately-fabricated proficiency judgment. "claimed" (zero real
# evidence) intentionally has no entry: there is nothing to base a level on yet.
_LEVEL_BY_STATE = {
    "verified": "Advanced",
    "strong": "Intermediate",
    "supported": "Beginner",
    "stale": "Needs Refresh",
}


class SkillNotFoundError(Exception):
    """The skill isn't part of the profile's current role's skill set."""


def _to_item_read(view: evidence_engine.SkillEvidenceView) -> SkillPassportItemRead:
    return SkillPassportItemRead(
        skill=view.skill,
        level=_LEVEL_BY_STATE.get(view.state),
        confidence_percent=round(view.confidence * 100),
        state=view.state,
        last_verified_at=view.last_verified_at,
        evidence=[
            EvidenceItemRead(source=i.source, source_id=i.source_id, label=i.label, score=i.score, detected_at=i.detected_at)
            for i in view.items
        ],
        next_action_hint=evidence_engine.next_action_hint(view.skill, view.state),
    )


def list_skills(db: Session, profile: StudentProfile) -> List[SkillPassportItemRead]:
    context = resolve_role_context(db, profile)
    if context is None or not context.weights:
        return []
    views = evidence_engine.get_all_skill_evidence(db, profile.id, list(context.weights.keys()))
    return [_to_item_read(v) for v in views]


def get_skill(db: Session, profile: StudentProfile, skill: str) -> SkillPassportItemRead:
    context = resolve_role_context(db, profile)
    if context is None or skill not in context.weights:
        raise SkillNotFoundError(f"Skill {skill!r} not found in your Skill Passport")
    view = evidence_engine.get_skill_evidence(db, profile.id, skill)
    return _to_item_read(view)
