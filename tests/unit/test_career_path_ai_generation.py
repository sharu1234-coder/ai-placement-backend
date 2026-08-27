from app.ai.provider import AIGenerationError
from app.ai.schemas.career_path import (
    GeneratedCareerPathExplanations,
    GeneratedRoleCatalog,
    GeneratedRoleOverview,
    GeneratedRolePhase,
    GeneratedRoleSkill,
    GeneratedSkillExplanation,
)
from app.services.career_path.ai_generation import (
    ExplainerSkillInput,
    generate_skill_explanations,
    get_or_generate_role_definition,
)
from app.services.career_path.role_catalog import PHASE_ORDER


class _SuccessProvider:
    def generate_structured(self, prompt, schema):
        if schema is GeneratedRoleCatalog:
            return GeneratedRoleCatalog(
                overview=GeneratedRoleOverview(
                    short_explanation="Builds things.",
                    responsibilities=["Do work"],
                    core_skills=["Skill A", "Skill B"],
                    tools=["Tool A"],
                    difficulty="moderate",
                    entry_level_expectations="Some baseline.",
                    interview_focus=["Fundamentals"],
                    portfolio_expectations="One project.",
                ),
                phases=[
                    GeneratedRolePhase(
                        phase_key="foundations",
                        skills=[GeneratedRoleSkill(skill_key="Skill A!", title="Skill A", level="beginner")],
                    ),
                    GeneratedRolePhase(
                        phase_key="not_a_real_phase",
                        skills=[GeneratedRoleSkill(skill_key="bogus", title="Bogus", level="beginner")],
                    ),
                ],
            )
        if schema is GeneratedCareerPathExplanations:
            return GeneratedCareerPathExplanations(
                explanations=[
                    GeneratedSkillExplanation(skill_key="skill_a", why_it_matters="Matters a lot."),
                    GeneratedSkillExplanation(skill_key="unknown_key", why_it_matters="Should be dropped."),
                ]
            )
        raise AssertionError(f"unexpected schema {schema}")

    def generate_text(self, prompt):
        return "fake"


class _FailProvider:
    def generate_structured(self, prompt, schema):
        raise AIGenerationError("boom")

    def generate_text(self, prompt):
        return "fake"


class _EmptyResultProvider:
    def generate_structured(self, prompt, schema):
        return GeneratedRoleCatalog(
            overview=GeneratedRoleOverview(
                short_explanation="x",
                responsibilities=[],
                core_skills=[],
                tools=[],
                difficulty="moderate",
                entry_level_expectations="x",
                interview_focus=[],
                portfolio_expectations="x",
            ),
            phases=[GeneratedRolePhase(phase_key="not_real", skills=[GeneratedRoleSkill(skill_key="x", title="x")])],
        )

    def generate_text(self, prompt):
        return "fake"


def test_ai_success_drops_unrecognized_phase_key():
    family, generated_via = get_or_generate_role_definition("Blockchain Developer", _SuccessProvider())
    assert generated_via == "ai_generated"
    assert set(family.phase_skill_templates.keys()) == {"foundations"}
    assert family.phase_skill_templates["foundations"][0].skill_key == "skill_a"
    # AI-assisted roles never generate their own project templates -- generic fallback used.
    assert len(family.project_templates) == 3


def test_ai_failure_falls_back_to_generic_template():
    family, generated_via = get_or_generate_role_definition("Petroleum Engineer", _FailProvider())
    assert generated_via == "fallback"
    assert set(family.phase_skill_templates.keys()).issubset(set(PHASE_ORDER))
    assert sum(len(v) for v in family.phase_skill_templates.values()) > 0
    assert len(family.project_templates) == 3
    assert "Petroleum Engineer" in family.display_name


def test_ai_returning_only_unrecognized_phases_falls_back_to_generic():
    family, generated_via = get_or_generate_role_definition("Astrobiologist", _EmptyResultProvider())
    assert generated_via == "fallback"
    assert family.phase_skill_templates.get("foundations") is not None


def test_explanations_validated_against_known_keys():
    items = [
        ExplainerSkillInput("skill_a", "Skill A", "foundations", "beginner"),
        ExplainerSkillInput("skill_c", "Skill C", "foundations", "beginner"),
    ]
    explanations = generate_skill_explanations("Blockchain Developer", _SuccessProvider(), items)
    assert explanations["skill_a"] == "Matters a lot."
    assert "unknown_key" not in explanations
    # skill_c wasn't in the AI's response -- must get the deterministic fallback, not be missing.
    assert "Skill C" in explanations["skill_c"]


def test_explanations_all_fallback_on_ai_failure():
    items = [ExplainerSkillInput("skill_a", "Skill A", "foundations", "beginner")]
    explanations = generate_skill_explanations("Blockchain Developer", _FailProvider(), items)
    assert "core part of becoming job-ready" in explanations["skill_a"]


def test_explanations_empty_items_returns_empty_dict():
    assert generate_skill_explanations("Blockchain Developer", _FailProvider(), []) == {}
