"""AI-assisted fallback for roles that aren't in role_catalog.ROLE_FAMILIES, plus the
shared "why it matters" explanation generator used for every role (curated or AI-assisted).

Follows this codebase's established AI-decoration pattern exactly (see
placement_path_service._generate_explanations, ats_check_service._apply_ai_overlay,
jd_service.generate_preparation_plan): compute deterministic facts and their keys first,
pass those keys into the prompt, and only accept AI output whose key exactly matches an
already-known key -- anything else is silently dropped. AI can decorate or fill a gap; it
can never introduce structure the deterministic layer didn't already define. On any
AIGenerationError, generation falls back to hand-authored, deterministic content and never
raises -- a Career Path must never fail to generate just because the AI call failed.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.career_path import GeneratedCareerPathExplanations, GeneratedRoleCatalog
from app.services.career_path.role_catalog import PHASE_ORDER, ProjectTemplate, RoleFamilyDef, SkillTemplate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExplainerSkillInput:
    """A skill item to request a "why it matters" note for, carrying enough context (phase)
    for the AI to write something grounded rather than generic filler."""

    skill_key: str
    title: str
    phase_key: str
    level: str


_ROLE_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "ai" / "prompts" / "career_path_role_v2.txt"
_EXPLAINER_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "ai" / "prompts" / "career_path_explainer_v3.txt"

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_PATTERN.sub("_", text.strip().lower()).strip("_")
    return slug or "role"


def derive_custom_family_key(role_name: str) -> str:
    """The stable "custom:<slug>" family_key a role_name resolves to when it has no curated
    catalog match. Pure and AI-free -- lets callers look up an already-generated CareerPath
    row for this role before deciding whether an AI call is even needed."""
    return f"custom:{_slugify(role_name)}"


def serialize_role_family(family: RoleFamilyDef) -> dict:
    """JSON-safe snapshot of a RoleFamilyDef's generated (non-overview) content -- its phase
    skill templates and project templates -- for persisting on a CareerPath row. Paired with
    rehydrate_role_family() so an AI-generated/fallback role's exact definition can be reused
    verbatim on the next select_role() call instead of re-invoking the AI, whose skill_keys
    are not guaranteed stable across separate calls."""
    return {
        "phase_skill_templates": {
            phase_key: [{"skill_key": s.skill_key, "title": s.title, "level": s.level} for s in templates]
            for phase_key, templates in family.phase_skill_templates.items()
        },
        "project_templates": [
            {
                "project_key": p.project_key,
                "title": p.title,
                "difficulty": p.difficulty,
                "skills_demonstrated": list(p.skills_demonstrated),
                "technologies": list(p.technologies),
                "problem_statement": p.problem_statement,
                "expected_outcome": p.expected_outcome,
                "what_should_be_included": p.what_should_be_included,
                "why_recruiters_value": p.why_recruiters_value,
                "resume_bullet_suggestion": p.resume_bullet_suggestion,
                "interview_questions": list(p.interview_questions),
            }
            for p in family.project_templates
        ],
    }


def rehydrate_role_family(family_key: str, display_name: str, role_overview: dict, generated_definition: dict) -> RoleFamilyDef:
    """Reconstructs a RoleFamilyDef from a persisted role_overview + generated_definition
    snapshot -- the inverse of serialize_role_family(). Used to reuse an already-generated
    custom role's exact definition (same skill_keys, same projects) without a fresh AI call."""
    phase_skill_templates = {
        phase_key: [SkillTemplate(skill_key=s["skill_key"], title=s["title"], level=s["level"]) for s in templates]
        for phase_key, templates in generated_definition.get("phase_skill_templates", {}).items()
    }
    project_templates = [
        ProjectTemplate(
            project_key=p["project_key"],
            title=p["title"],
            difficulty=p["difficulty"],
            skills_demonstrated=list(p["skills_demonstrated"]),
            technologies=list(p["technologies"]),
            problem_statement=p["problem_statement"],
            expected_outcome=p["expected_outcome"],
            what_should_be_included=p["what_should_be_included"],
            why_recruiters_value=p["why_recruiters_value"],
            resume_bullet_suggestion=p["resume_bullet_suggestion"],
            interview_questions=list(p["interview_questions"]),
        )
        for p in generated_definition.get("project_templates", [])
    ]
    return RoleFamilyDef(
        family_key=family_key,
        display_name=display_name,
        category=role_overview.get("category", "Other"),
        aliases=frozenset({display_name.lower()}),
        short_explanation=role_overview.get("short_explanation", ""),
        responsibilities=list(role_overview.get("responsibilities", [])),
        core_skills=list(role_overview.get("core_skills", [])),
        tools=list(role_overview.get("tools", [])),
        difficulty=role_overview.get("difficulty", "moderate"),
        entry_level_expectations=role_overview.get("entry_level_expectations", ""),
        interview_focus=list(role_overview.get("interview_focus", [])),
        portfolio_expectations=role_overview.get("portfolio_expectations", ""),
        phase_skill_templates=phase_skill_templates,
        project_templates=project_templates,
    )


def _generic_project_templates(role_name: str) -> List[ProjectTemplate]:
    """A deliberately generic-but-honest fallback project set for roles with no curated or
    AI-generated project content -- never zero content, never a fabricated-sounding claim.
    Used for every AI-assisted/fallback role, regardless of whether the role definition
    itself came from AI or the hand-authored generic template."""
    return [
        ProjectTemplate(
            project_key="portfolio_project",
            title=f"{role_name} Portfolio Project",
            difficulty="beginner",
            skills_demonstrated=["Applying core skills for this role"],
            technologies=["Tools relevant to this role"],
            problem_statement=(
                f"Pick a small, real problem relevant to {role_name} and work through it "
                "end-to-end, using the skills from your Foundations and Core Skills phases."
            ),
            expected_outcome="A completed piece of work you can show and explain in an interview.",
            what_should_be_included="A clear problem statement, your approach, and a summary of the outcome.",
            why_recruiters_value="Shows you can apply what you've learned to a real problem, not just study it.",
            resume_bullet_suggestion=(f"Completed a self-directed project applying core {role_name} skills to a real problem."),
            interview_questions=[
                "Walk me through the problem you chose and why.",
                "What was the hardest part, and how did you solve it?",
                "What would you do differently next time?",
            ],
        ),
        ProjectTemplate(
            project_key="case_study",
            title=f"{role_name} Case Study",
            difficulty="intermediate",
            skills_demonstrated=["Analysis and structured problem-solving"],
            technologies=["Tools relevant to this role"],
            problem_statement=(
                f"Research a real situation a {role_name} might face and write up a structured "
                "case study: the problem, your analysis, and your recommendation."
            ),
            expected_outcome="A written case study you can walk an interviewer through.",
            what_should_be_included="Context, your analysis process, and a clearly justified recommendation.",
            why_recruiters_value=(
                "Case studies show structured thinking -- exactly what interviews for this kind of role probe for."
            ),
            resume_bullet_suggestion=(
                f"Authored a case study analyzing a real-world {role_name} scenario and proposing a solution."
            ),
            interview_questions=[
                "How did you structure your analysis?",
                "What alternative approaches did you consider?",
                "How would you validate your recommendation?",
            ],
        ),
        ProjectTemplate(
            project_key="applied_mini_project",
            title=f"Applied {role_name} Mini-Project",
            difficulty="advanced",
            skills_demonstrated=["End-to-end application of core skills"],
            technologies=["Tools relevant to this role"],
            problem_statement=(
                f"Take on a slightly larger, more ambiguous problem in the {role_name} space "
                "and see it through from start to finish."
            ),
            expected_outcome="A finished piece of work that reflects your strongest current skill level.",
            what_should_be_included="Your process, the decisions you made, and honest reflection on the result.",
            why_recruiters_value="A more ambitious, self-directed project signals initiative and readiness for real work.",
            resume_bullet_suggestion=f"Independently completed an applied {role_name} project from scoping through delivery.",
            interview_questions=[
                "What made this project more challenging than your earlier ones?",
                "How did you handle the ambiguity in scoping it?",
                "What feedback would you want from a mentor on this work?",
            ],
        ),
    ]


def _generic_role_family(family_key: str, role_name: str) -> RoleFamilyDef:
    """Hand-authored, deterministic fallback used when AI generation fails or returns an
    empty/invalid result -- never raises, never blocks, never surfaces malformed text."""
    return RoleFamilyDef(
        family_key=family_key,
        display_name=role_name,
        category="Other",
        aliases=frozenset({role_name.lower()}),
        short_explanation=(
            f"A {role_name} applies role-specific skills and judgment to do focused, high-quality work in this field."
        ),
        responsibilities=[
            "Understand and apply the core skills this role requires",
            "Communicate clearly with teammates and stakeholders",
            "Deliver reliable, high-quality work",
            "Continuously build domain knowledge relevant to this role",
        ],
        core_skills=["Domain Fundamentals", "Communication", "Problem Solving", "Portfolio Building"],
        tools=["Tools relevant to this specific role"],
        difficulty="moderate",
        entry_level_expectations=(
            "A solid grasp of this role's fundamentals and at least one piece of real, "
            "reviewable work to discuss in an interview."
        ),
        interview_focus=["Role fundamentals", "Past work walkthrough", "Communication", "Behavioral questions"],
        portfolio_expectations="At least one real, well-documented piece of work relevant to this role.",
        phase_skill_templates={
            "foundations": [
                SkillTemplate("role_fundamentals", f"{role_name} Fundamentals", "beginner"),
                SkillTemplate("communication_basics", "Professional Communication", "beginner"),
            ],
            "core_skills": [
                SkillTemplate("core_domain_skill", "Core Domain Skill Building", "intermediate"),
                SkillTemplate("problem_solving_practice", "Applied Problem Solving", "intermediate"),
            ],
            "interview_prep": [
                SkillTemplate("interview_fundamentals_prep", "Interview Fundamentals Prep", "advanced"),
                SkillTemplate("behavioral_prep_generic", "Behavioral Interview Prep", "intermediate"),
            ],
            "job_ready": [
                SkillTemplate("portfolio_polish_generic", "Portfolio & Resume Polish", "intermediate"),
                SkillTemplate("mock_interview_loop_generic", "Full Mock Interview Loop", "advanced"),
            ],
        },
        project_templates=_generic_project_templates(role_name),
    )


def _build_role_prompt(role_name: str) -> str:
    template = _ROLE_PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(role_name=role_name)


def get_or_generate_role_definition(role_name: str, ai_provider: AIProvider) -> Tuple[RoleFamilyDef, str]:
    """Called only when role_catalog.resolve_role_family found no curated match. Tries one
    AI call for a full role definition; on any failure, or a response with zero usable
    phases, falls back to the generic hand-authored template. Never raises. Returns
    (family, generated_via) where generated_via is "ai_generated" or "fallback" -- callers
    persist this on the CareerPath row for transparency (e.g. downstream "estimated" labeling
    on the AI-assisted tier's job-readiness numbers)."""
    family_key = derive_custom_family_key(role_name)
    try:
        prompt = _build_role_prompt(role_name)
        generated: GeneratedRoleCatalog = ai_provider.generate_structured(prompt, GeneratedRoleCatalog)

        phase_skill_templates: Dict[str, List[SkillTemplate]] = {}
        for phase in generated.phases:
            if phase.phase_key not in PHASE_ORDER:
                continue  # unrecognized phase key -- silently dropped, never trusted
            phase_skill_templates[phase.phase_key] = [
                SkillTemplate(_slugify(s.skill_key) or s.skill_key, s.title, s.level) for s in phase.skills
            ]

        total_skills = sum(len(v) for v in phase_skill_templates.values())
        if total_skills == 0:
            raise AIGenerationError("AI returned no usable phase/skill content")

        overview = generated.overview
        family = RoleFamilyDef(
            family_key=family_key,
            display_name=role_name,
            category="Other",
            aliases=frozenset({role_name.lower()}),
            short_explanation=overview.short_explanation,
            responsibilities=list(overview.responsibilities),
            core_skills=list(overview.core_skills),
            tools=list(overview.tools),
            difficulty=overview.difficulty,
            entry_level_expectations=overview.entry_level_expectations,
            interview_focus=list(overview.interview_focus),
            portfolio_expectations=overview.portfolio_expectations,
            phase_skill_templates=phase_skill_templates,
            project_templates=_generic_project_templates(role_name),
        )
        return family, "ai_generated"
    except AIGenerationError as exc:
        logger.warning("AI role-family generation failed for role_family_key=%s, using fallback: %s", family_key, exc)
        return _generic_role_family(family_key, role_name), "fallback"


def _build_explainer_prompt(role_name: str, items: Sequence[ExplainerSkillInput]) -> str:
    lines = [f"{item.skill_key} | {item.title} | {item.phase_key} | {item.level}" for item in items]
    template = _EXPLAINER_PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(role_name=role_name, skill_lines="\n".join(lines))


def _fallback_skill_explanation(role_name: str, item: ExplainerSkillInput) -> str:
    return f"Builds your {item.title} skills -- a core part of becoming job-ready as a {role_name}."


def generate_skill_explanations(role_name: str, ai_provider: AIProvider, items: Sequence[ExplainerSkillInput]) -> Dict[str, str]:
    """Never blocks path generation: on any AI failure, or a partial/invalid response, the
    affected skills fall back to a deterministic template sentence. Used for every role,
    curated or AI-assisted -- AI here is decorative (explanatory text only), never structural.
    """
    valid_keys = {item.skill_key for item in items}
    explanations: Dict[str, str] = {}
    if items:
        try:
            prompt = _build_explainer_prompt(role_name, items)
            generated: GeneratedCareerPathExplanations = ai_provider.generate_structured(prompt, GeneratedCareerPathExplanations)
            for explanation in generated.explanations:
                if explanation.skill_key in valid_keys:
                    explanations[explanation.skill_key] = explanation.why_it_matters
        except AIGenerationError as exc:
            logger.warning("AI skill-explanation generation failed for role=%s, using template text: %s", role_name, exc)

    for item in items:
        if item.skill_key not in explanations:
            explanations[item.skill_key] = _fallback_skill_explanation(role_name, item)
    return explanations
