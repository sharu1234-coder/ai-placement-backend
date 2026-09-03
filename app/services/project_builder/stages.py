"""The fixed 10-stage build process every ProjectBuild follows, matching the product spec's
reference UI exactly. Pure, deterministic, zero DB/AI access -- same purity discipline as
career_path.role_catalog's PHASE_ORDER. Two-tier content, mirroring role_catalog's own split:
STAGE_TITLES (structure, always fixed) is separate from per-build guidance text, which is
AI-assisted with the FALLBACK_GUIDANCE below as the deterministic, never-fabricated floor.
"""

from typing import Dict, List

STAGE_ORDER: List[str] = [
    "understand_problem",
    "architecture",
    "ingestion",
    "processing",
    "storage",
    "testing",
    "deployment",
    "documentation",
    "github_readme",
    "interview_prep",
]

STAGE_TITLES: Dict[str, str] = {
    "understand_problem": "Understand the Problem",
    "architecture": "Architecture",
    "ingestion": "Data/Input Ingestion",
    "processing": "Processing Logic",
    "storage": "Storage",
    "testing": "Testing",
    "deployment": "Deployment",
    "documentation": "Documentation",
    "github_readme": "GitHub README",
    "interview_prep": "Interview Prep",
}

# Hand-authored, deterministic fallback guidance -- used whenever AI generation fails or
# returns an unusable result. Generic-but-honest, same spirit as career_path.ai_generation's
# _generic_project_templates: never zero content, never a fabricated-sounding claim.
FALLBACK_GUIDANCE: Dict[str, str] = {
    "understand_problem": (
        "Write down, in your own words, exactly what problem this project solves and for "
        "whom. If you can't explain it in two sentences, it isn't scoped yet."
    ),
    "architecture": (
        "Sketch the major pieces of your system and how data or requests flow between them "
        "before writing implementation code. A rough diagram is enough."
    ),
    "ingestion": (
        "Decide where your project's input or data comes from and how you'll bring it in "
        "reliably, including what happens when that input is missing or malformed."
    ),
    "processing": (
        "Implement the core logic that turns your input into your intended output. Keep this "
        "stage focused on correctness first, polish later."
    ),
    "storage": (
        "Decide what state your project needs to persist, if any, and why. It's fine to "
        "conclude nothing needs to be stored -- justify that choice either way."
    ),
    "testing": (
        "Verify your project actually works, including at least one edge case or failure "
        "scenario, not just the happy path."
    ),
    "deployment": (
        "Get your project running somewhere it can actually be used or demonstrated, even if "
        "that's just running reliably on your own machine with clear run instructions."
    ),
    "documentation": (
        "Write down how your project works and how to run it while the details are still "
        "fresh -- this becomes the backbone of your README."
    ),
    "github_readme": (
        "Turn your documentation into a real README: what it does, how it works, and the "
        "tech you used, in a form a recruiter skimming your GitHub could understand in under "
        "a minute."
    ),
    "interview_prep": (
        "Practice explaining this project out loud: what it does, why you made the choices "
        "you made, and what you'd improve given more time."
    ),
}
