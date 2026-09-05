"""Project -> Proof: once a ProjectBuild's final stage is completed, turns the finished build
into interview-ready artifacts (a resume bullet, a README draft, two spoken-style
explanations, and a set of defense questions). Follows the same AI-decoration pattern as
career_path.ai_generation and project_builder.ai_generation: on any AIGenerationError, or an
unusable response, falls back to a deterministic, hand-authored template and never raises --
a build must never fail to complete just because proof generation failed. Results are cached
directly on the ProjectBuild row (see its docstring), not a separate table.

The student's own per-stage notes are genuinely untrusted free text fed into this prompt (see
project_builder_proof_v2.txt's SECURITY RULE) -- output_guard is applied to every generated
text field before it's trusted, same backstop used everywhere else in this codebase's AI
overlays.
"""

import logging
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
from app.ai.output_guard import looks_like_prompt_leak
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.project_builder import GeneratedProjectProof
from app.models.project_build import ProjectBuild
from app.models.project_build_stage import ProjectBuildStage
from app.services.project_builder.stages import STAGE_TITLES

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "project_builder_proof_v2.txt"

_FALLBACK_DEFENSE_QUESTIONS = [
    "Walk me through how this project works, end to end.",
    "What was the hardest part of building this, and how did you solve it?",
    "Why did you make the technical choices you made?",
    "What would you improve if you kept working on this?",
]


def _build_prompt(role_name: str, title: str, stack_summary: Optional[str], stages: List[ProjectBuildStage]) -> str:
    stage_notes = "\n".join(
        f"{STAGE_TITLES[s.stage_key]} | {s.notes or '(no notes)'}" for s in sorted(stages, key=lambda s: s.order_index)
    )
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return render_prompt(
        template,
        role_name=role_name, title=title, stack_summary=stack_summary or "(not specified)", stage_notes=stage_notes
    )


def _fallback_proof(role_name: str, title: str) -> GeneratedProjectProof:
    return GeneratedProjectProof(
        resume_bullet=f"Built {title}, an independent project applying core {role_name} skills end-to-end.",
        readme_markdown=(
            f"# {title}\n\n"
            f"A project built while preparing for a {role_name} role.\n\n"
            "## What it does\n(Add a short description here.)\n\n"
            "## How it works\n(Add a short technical overview here.)\n\n"
            "## Tech used\n(List the tools and technologies you used.)\n"
        ),
        explanation_60s=(
            f"I built {title} as a hands-on project to practice the core skills a {role_name} "
            "needs, working through it from the initial problem through to a finished result."
        ),
        explanation_5min=(
            f"I built {title} as a self-directed project targeting {role_name} skills. I worked "
            "through understanding the problem, designing the approach, implementing it, "
            "testing it, and documenting it, making deliberate decisions at each stage and "
            "learning from the parts that were harder than expected."
        ),
        defense_questions=list(_FALLBACK_DEFENSE_QUESTIONS),
    )


def _looks_safe(proof: GeneratedProjectProof) -> bool:
    fields = [proof.resume_bullet, proof.readme_markdown, proof.explanation_60s, proof.explanation_5min]
    return not any(looks_like_prompt_leak(f) for f in fields) and not any(
        looks_like_prompt_leak(q) for q in proof.defense_questions
    )


def generate_proof(db: Session, build: ProjectBuild, stages: List[ProjectBuildStage], ai_provider: AIProvider) -> None:
    role_name = build.target_role_name or "this role"
    try:
        prompt = _build_prompt(role_name, build.title, build.stack_summary, stages)
        generated: GeneratedProjectProof = ai_provider.generate_structured(prompt, GeneratedProjectProof)
        if not generated.defense_questions or not _looks_safe(generated):
            raise AIGenerationError("AI proof output was empty or contained prompt-leak markers")
    except AIGenerationError as exc:
        logger.warning("AI project-proof generation failed for build_id=%s, using template fallback: %s", build.id, exc)
        generated = _fallback_proof(role_name, build.title)

    build.proof_resume_bullet = generated.resume_bullet
    build.proof_readme_markdown = generated.readme_markdown
    build.proof_explanation_60s = generated.explanation_60s
    build.proof_explanation_5min = generated.explanation_5min
    build.proof_defense_questions = list(generated.defense_questions)
    db.commit()
