"""Skill Graph -- a pure, backend-only conceptual model connecting a target role to its
required skills. Per the product spec (Skill Graph feature), this deliberately stays
backend-only: no giant graph UI, just a read function every new intelligence-layer service
(Evidence Engine, Career Twin) can lean on instead of each re-deriving "which skills matter
for this role, and how much" its own way.

Two independent sources feed a role's skill set, matching how the app already models roles in
two places that never talk to each other:
  - RoleSkillWeight rows (role_id FK) -- the older, curated Role catalog used by Assessments/
    Roadmap/Placement Alarm.
  - Career Path's RoleFamilyDef.core_skills/tools (role_catalog.py) -- keyed by role_family_key,
    used by Career Path only.

get_role_skill_weights() normalizes both into one {skill: weight} view. Deliberately does NOT
import anything from career_path/service.py (only role_catalog, which is already the generic,
dependency-free layer within that package) -- Skill Graph must not become coupled to Career
Path's orchestration, mirroring the same independence discipline Career Path itself applies to
Roadmap/Placement Path.
"""

from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.models.role_skill_weight import RoleSkillWeight
from app.services.career_path.role_catalog import get_role_family

# Core skills are what the role is actually evaluated on; tools are supporting/nice-to-have --
# weighted lower so a missing tool never outranks a missing core skill in downstream ranking
# (Next Best Action, Career Twin's biggest-gap).
_TOOL_WEIGHT = 0.6
_CORE_SKILL_WEIGHT = 1.0


def get_role_skill_weights_by_role_id(db: Session, role_id: int) -> Dict[str, float]:
    """skill -> weight for the older curated Role catalog (RoleSkillWeight rows)."""
    rows = db.query(RoleSkillWeight).filter(RoleSkillWeight.role_id == role_id).all()
    return {row.skill: row.weight for row in rows}


def get_role_skill_weights_from_overview(role_overview: dict) -> Dict[str, float]:
    """skill -> weight computed directly from a CareerPath.role_overview snapshot (its
    core_skills + tools lists), rather than a role_family_key catalog lookup. This is what a
    caller holding a specific CareerPath row (e.g. career_twin_service) should prefer over
    get_role_skill_weights_by_family_key: role_overview is the authoritative persisted
    snapshot of what the student was actually shown (see CareerPath.role_overview's own
    docstring), and -- unlike role_catalog.get_role_family -- it covers AI-assisted/custom
    roles too, which have no entry in the static ROLE_FAMILIES dict."""
    weights = {skill: _CORE_SKILL_WEIGHT for skill in role_overview.get("core_skills", [])}
    for tool in role_overview.get("tools", []):
        weights.setdefault(tool, _TOOL_WEIGHT)
    return weights


def get_role_skill_weights_by_family_key(role_family_key: str) -> Dict[str, float]:
    """skill -> weight for a Career Path role family. No DB access -- role_catalog is a pure,
    static Python dict for the curated tier. An unrecognized role_family_key (e.g. an
    AI-assisted/custom role not registered in ROLE_FAMILIES) returns {} -- callers that hold
    the actual CareerPath row should prefer get_role_skill_weights_from_overview instead,
    which works for every tier."""
    family = get_role_family(role_family_key)
    if family is None:
        return {}
    return get_role_skill_weights_from_overview({"core_skills": family.core_skills, "tools": family.tools})


def get_role_skill_weights(
    db: Session, role_id: Optional[int] = None, role_family_key: Optional[str] = None
) -> Dict[str, float]:
    """Combined view -- callers pass whichever identifier(s) they have (a StudentProfile's
    target_role_id, a CareerPath's role_family_key, or both). If both are given, family-key
    weights take precedence for any skill they define (Career Path content is the more
    current, role-specific source for a student actively using it), falling back to the
    RoleSkillWeight catalog for anything the family definition doesn't mention."""
    weights: Dict[str, float] = {}
    if role_id is not None:
        weights.update(get_role_skill_weights_by_role_id(db, role_id))
    if role_family_key is not None:
        weights.update(get_role_skill_weights_by_family_key(role_family_key))
    return weights
