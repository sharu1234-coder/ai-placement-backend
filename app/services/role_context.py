"""Shared "which role, and which skills does it need" resolution -- used by Career Twin and
Skill Passport so they always agree on the current role without either depending on the
other's code (both instead depend on this generic layer, plus skill_graph/evidence_engine --
the same independence discipline Career Path/Placement Path/Roadmap already follow).

Prefers a profile's current (is_primary) Career Path; falls back to their legacy
StudentProfile.target_role_id when no Career Path exists yet. Returns None when neither is
set -- callers must never fabricate a default role.
"""

from typing import Dict, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.career_path import CareerPath
from app.models.profile import StudentProfile
from app.models.role import Role
from app.services import skill_graph


class RoleContext(BaseModel):
    role_family_key: str  # stable key, also used for ReadinessSnapshot rows
    display_role_name: str
    role_source: str  # career_path | target_role
    is_estimate: bool
    weights: Dict[str, float]


def resolve_role_context(db: Session, profile: StudentProfile) -> Optional[RoleContext]:
    path = db.query(CareerPath).filter(CareerPath.profile_id == profile.id, CareerPath.is_primary.is_(True)).first()
    if path is not None:
        weights = skill_graph.get_role_skill_weights_from_overview(path.role_overview or {})
        return RoleContext(
            role_family_key=path.role_family_key,
            display_role_name=path.display_role_name,
            role_source="career_path",
            is_estimate=(path.generated_via != "catalog"),
            weights=weights,
        )

    if profile.target_role_id is not None:
        role = db.query(Role).filter(Role.id == profile.target_role_id).first()
        if role is not None:
            weights = skill_graph.get_role_skill_weights_by_role_id(db, role.id)
            return RoleContext(
                role_family_key=f"role:{role.id}",
                display_role_name=role.name,
                role_source="target_role",
                is_estimate=False,
                weights=weights,
            )

    return None
