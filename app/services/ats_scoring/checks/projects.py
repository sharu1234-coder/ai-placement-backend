import re
from typing import Any, Dict, List

from app.services.ats_scoring.types import CategoryResult, IssueDraft, clamp_score

_QUANTIFIED_RE = re.compile(r"\d|%|\$|₹")
_WEAK_OPENERS = (
    "responsible for", "worked on", "helped with", "duties included",
    "in charge of", "tasked with", "involved in", "assisted with",
)
_MIN_DESCRIPTION_WORDS = 8

_EMPTY_DESCRIPTION_PENALTY = 20
_LOW_QUANTIFICATION_PENALTY = 15
_SHORT_DESCRIPTION_PENALTY = 10
_NO_TECH_STACK_PENALTY = 10


def check_projects(extracted_projects: List[Dict[str, Any]]) -> CategoryResult:
    """Category F -- Projects & Achievements (10%). Mirrors experience.py's content
    checks, plus tech_stack population (a real, already-extracted signal of whether the
    project entry names concrete tools/technologies used)."""
    if not extracted_projects:
        return CategoryResult(
            category="projects",
            score=65,
            summary="No projects were detected. For students especially, a couple of well-described "
            "projects can strongly demonstrate hands-on skill.",
        )

    score = 100.0
    issues: List[IssueDraft] = []
    strengths: List[str] = []
    improvements: List[str] = []

    descriptions = [(p.get("description") or "").strip() for p in extracted_projects]
    empty_count = sum(1 for d in descriptions if not d)
    if empty_count:
        score -= min(_EMPTY_DESCRIPTION_PENALTY, empty_count * 10)
        improvements.append(f"{empty_count} project(s) have no description")
        issues.append(
            IssueDraft(
                category="projects",
                severity="high",
                title="Missing project descriptions",
                description=f"{empty_count} of your projects don't have any description.",
                how_to_fix="Add a couple of sentences on what the project does and what you built.",
                points_recoverable=min(_EMPTY_DESCRIPTION_PENALTY, empty_count * 10),
            )
        )

    non_empty = [d for d in descriptions if d]
    if non_empty:
        short = sum(1 for d in non_empty if len(d.split()) < _MIN_DESCRIPTION_WORDS)
        if short:
            score -= _SHORT_DESCRIPTION_PENALTY
            improvements.append("Some project descriptions are very brief")
            issues.append(
                IssueDraft(
                    category="projects",
                    severity="medium",
                    title="Very short project descriptions",
                    description=f"{short} project description(s) are quite brief.",
                    how_to_fix="Describe what the project does, your role, and any outcome or result.",
                    points_recoverable=_SHORT_DESCRIPTION_PENALTY,
                )
            )

        quantified = sum(1 for d in non_empty if _QUANTIFIED_RE.search(d))
        if quantified / len(non_empty) < 0.25:
            score -= _LOW_QUANTIFICATION_PENALTY
            improvements.append("Project descriptions lack measurable detail")
            issues.append(
                IssueDraft(
                    category="projects",
                    severity="medium",
                    title="Add measurable detail to your projects",
                    description="Most project descriptions don't include a concrete number or outcome.",
                    how_to_fix="Mention scale where truthful -- users served, data processed, performance improved, etc.",
                    points_recoverable=_LOW_QUANTIFICATION_PENALTY,
                )
            )
        else:
            strengths.append("Project descriptions include concrete detail")

        weak = sum(1 for d in non_empty if d.lower().startswith(_WEAK_OPENERS))
        if weak / len(non_empty) <= 0.3:
            strengths.append("Uses action verbs to describe your projects")

    tech_stack_count = sum(1 for p in extracted_projects if (p.get("tech_stack") or "").strip())
    if tech_stack_count < len(extracted_projects):
        missing = len(extracted_projects) - tech_stack_count
        score -= _NO_TECH_STACK_PENALTY
        improvements.append(f"{missing} project(s) don't list the technologies used")
        issues.append(
            IssueDraft(
                category="projects",
                severity="low",
                title="Missing tech stack",
                description=f"{missing} of your projects don't name the tools or technologies used.",
                how_to_fix="Add a short tech-stack line to each project (e.g. 'Python, FastAPI, PostgreSQL').",
                points_recoverable=_NO_TECH_STACK_PENALTY,
            )
        )
    else:
        strengths.append("Every project lists the technologies used")

    final_score = clamp_score(score)
    if final_score >= 90:
        summary = "Your projects are well-organized and clearly demonstrate hands-on skill."
    elif final_score >= 70:
        summary = "Your projects are a solid asset, with a little more detail they could stand out even more."
    else:
        summary = "Your projects could show more concrete detail about what you built and the technologies you used."

    return CategoryResult(
        category="projects",
        score=final_score,
        summary=summary,
        strengths=strengths,
        improvements=improvements,
        how_to_improve=[issue.how_to_fix for issue in issues],
        issues=issues,
    )
