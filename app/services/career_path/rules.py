"""Pure deterministic rules for assembling and scoring a CareerPath. Zero DB/AI access --
every function here is a pure function of its inputs, mirroring placement_path_rules.py's
purity discipline but implemented independently (Career Path must not import from it, or
depend on Roadmap's code in any way).
"""

from dataclasses import dataclass
from typing import List, Literal, Tuple

from app.services.career_path.role_catalog import PHASE_OBJECTIVES, PHASE_ORDER, PHASE_TITLES, RoleFamilyDef

EffectiveStatus = Literal["not_started", "in_progress", "completed"]


@dataclass(frozen=True)
class SkillItemSpec:
    phase_key: str
    skill_key: str
    title: str
    level: str
    order_index: int


@dataclass(frozen=True)
class ProjectSpec:
    project_key: str
    title: str
    difficulty: str
    skills_demonstrated: List[str]
    technologies: List[str]
    problem_statement: str
    expected_outcome: str
    what_should_be_included: str
    why_recruiters_value: str
    resume_bullet_suggestion: str
    interview_questions: List[str]
    order_index: int


@dataclass(frozen=True)
class PhaseSpec:
    phase_key: str
    title: str
    objective: str
    order_index: int
    skill_items: List[SkillItemSpec]
    projects: List[ProjectSpec]  # only ever non-empty for the "projects" phase


def build_phase_specs(role_family: RoleFamilyDef) -> List[PhaseSpec]:
    """Assembles the 5 fixed phases from a RoleFamilyDef's phase_skill_templates and
    project_templates. Pure -- the same role_family always produces the same specs, which
    is what makes carry-forward-by-key safe on regeneration (see career_path/service.py)."""
    specs: List[PhaseSpec] = []
    for order_index, phase_key in enumerate(PHASE_ORDER):
        skill_templates = role_family.phase_skill_templates.get(phase_key, [])
        skill_items = [
            SkillItemSpec(
                phase_key=phase_key,
                skill_key=template.skill_key,
                title=template.title,
                level=template.level,
                order_index=i,
            )
            for i, template in enumerate(skill_templates)
        ]

        projects: List[ProjectSpec] = []
        if phase_key == "projects":
            projects = [
                ProjectSpec(
                    project_key=p.project_key,
                    title=p.title,
                    difficulty=p.difficulty,
                    skills_demonstrated=list(p.skills_demonstrated),
                    technologies=list(p.technologies),
                    problem_statement=p.problem_statement,
                    expected_outcome=p.expected_outcome,
                    what_should_be_included=p.what_should_be_included,
                    why_recruiters_value=p.why_recruiters_value,
                    resume_bullet_suggestion=p.resume_bullet_suggestion,
                    interview_questions=list(p.interview_questions),
                    order_index=i,
                )
                for i, p in enumerate(role_family.project_templates)
            ]

        specs.append(
            PhaseSpec(
                phase_key=phase_key,
                title=PHASE_TITLES[phase_key],
                objective=PHASE_OBJECTIVES[phase_key],
                order_index=order_index,
                skill_items=skill_items,
                projects=projects,
            )
        )
    return specs


def compute_phase_status(item_statuses: List[str], project_statuses: List[str]) -> EffectiveStatus:
    """completed iff every child (skill items + projects in this phase) is completed;
    in_progress iff any child has been started but not all are completed; else not_started.
    Deliberately simpler than PlacementPathTopic's prerequisite-chain locking -- Career Path
    has no cross-item locking within a phase, matching the simpler progression model the
    product spec describes (a phase is just "done / in progress / not started")."""
    all_statuses = item_statuses + project_statuses
    if not all_statuses:
        return "not_started"
    if all(s == "completed" for s in all_statuses):
        return "completed"
    if any(s in ("completed", "in_progress") for s in all_statuses):
        return "in_progress"
    return "not_started"


def compute_progress(item_statuses: List[str], project_statuses: List[str]) -> Tuple[int, int]:
    """(completed_count, total_count) across every skill item + project in a CareerPath."""
    all_statuses = item_statuses + project_statuses
    completed = sum(1 for s in all_statuses if s == "completed")
    return completed, len(all_statuses)
