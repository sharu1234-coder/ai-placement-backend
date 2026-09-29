"""AI-assisted stage guidance for a freshly-created ProjectBuild. Follows this codebase's
established AI-decoration pattern exactly (see career_path.ai_generation.generate_skill_
explanations): the 10 stage_keys are already fixed and known (stages.STAGE_ORDER) before the
AI call is ever made; the AI only fills in guidance_text for each, keyed back to a stage_key
it must echo verbatim. Any stage_key the AI invents or omits is silently dropped/backfilled --
AI can decorate, never introduce structure. On any AIGenerationError, or an unusable/leaked
response, every affected stage falls back to stages.FALLBACK_GUIDANCE and generation never
raises -- a ProjectBuild must never fail to create just because the AI call failed.
"""

import logging
from pathlib import Path
from typing import Dict, Optional

from app.ai.input_guard import render_prompt
from app.ai.output_guard import contains_prompt_leak
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.project_builder import GeneratedStageGuidanceSet
from app.services.project_builder.stages import FALLBACK_GUIDANCE, STAGE_ORDER, STAGE_TITLES

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "ai" / "prompts" / "project_builder_stage_guidance_v2.txt"


def _build_prompt(role_name: str, title: str, stack_summary: Optional[str]) -> str:
    stage_lines = "\n".join(f"{key} | {STAGE_TITLES[key]}" for key in STAGE_ORDER)
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return render_prompt(
        template,
        role_name=role_name, title=title, stack_summary=stack_summary or "(not specified)", stage_lines=stage_lines
    )


def generate_stage_guidance(
    role_name: str, title: str, stack_summary: Optional[str], ai_provider: AIProvider
) -> Dict[str, str]:
    guidance: Dict[str, str] = {}
    try:
        prompt = _build_prompt(role_name, title, stack_summary)
        generated: GeneratedStageGuidanceSet = ai_provider.generate_structured(prompt, GeneratedStageGuidanceSet)
        for stage in generated.stages:
            if stage.stage_key not in STAGE_TITLES:
                continue  # unrecognized stage_key -- silently dropped, never trusted
            if contains_prompt_leak(stage):
                continue  # code-level backstop -- see app/ai/output_guard.py
            guidance[stage.stage_key] = stage.guidance_text
    except AIGenerationError as exc:
        logger.warning("AI stage-guidance generation failed for title=%r, using template fallback: %s", title, exc)

    for stage_key in STAGE_ORDER:
        if stage_key not in guidance:
            guidance[stage_key] = FALLBACK_GUIDANCE[stage_key]
    return guidance
