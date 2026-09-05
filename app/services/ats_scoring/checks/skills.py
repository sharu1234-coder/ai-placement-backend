import re
from typing import List

from app.services.ats_scoring.section_split import split_into_sections
from app.services.ats_scoring.types import CategoryResult, IssueDraft, clamp_score

_MIN_SKILL_COUNT = 5
_TOO_FEW_SKILLS_PENALTY = 20
_DUPLICATE_PENALTY = 10
_UNLISTED_PENALTY = 10

# A small, curated set of common technical terms used only to spot a real, checkable
# signal ("this term appears in your experience/projects but not in your skills list") --
# deliberately not an attempt at an exhaustive or authoritative skills taxonomy, and never
# used to judge whether a skill is "in demand."
_COMMON_TECH_TERMS = (
    "python", "java", "javascript", "typescript", "c++", "c#", "sql", "html", "css",
    "react", "angular", "vue", "node", "django", "flask", "fastapi", "spring",
    "aws", "azure", "gcp", "docker", "kubernetes", "git", "linux", "mongodb",
    "postgresql", "mysql", "redis", "kafka", "pandas", "numpy", "tensorflow",
    "pytorch", "kotlin", "swift", "go", "rust", "graphql", "rest api", "figma",
)

# Known aliases that would otherwise look like distinct skills after simple normalization.
_ALIAS_MAP = {
    "js": "javascript", "ts": "typescript", "reactjs": "react", "react.js": "react",
    "nodejs": "node", "node.js": "node", "py": "python", "postgres": "postgresql",
    "ml": "machine learning", "ai": "artificial intelligence",
}


def _normalize(skill: str) -> str:
    key = re.sub(r"[^a-z0-9+#. ]", "", skill.lower()).strip()
    return _ALIAS_MAP.get(key, key)


def check_skills(extracted_skills: List[str], raw_text: str) -> CategoryResult:
    """Category D -- Skills & Technical Coverage (15%). Reuses ResumeAnalysis's
    already-extracted skills list (no second extraction pass) plus a lightweight raw-text
    scan for common terms mentioned elsewhere but not listed as a skill."""
    score = 100.0
    issues: List[IssueDraft] = []
    strengths: List[str] = []
    improvements: List[str] = []

    if len(extracted_skills) < _MIN_SKILL_COUNT:
        score -= _TOO_FEW_SKILLS_PENALTY
        improvements.append("Very few skills listed")
        issues.append(
            IssueDraft(
                category="skills",
                severity="high",
                title="Too few skills listed",
                description=f"Only {len(extracted_skills)} skill(s) were detected on your resume.",
                how_to_fix="List the specific languages, tools, and technologies you've actually used.",
                points_recoverable=_TOO_FEW_SKILLS_PENALTY,
            )
        )
    else:
        strengths.append(f"{len(extracted_skills)} skills listed")

    # Real misalignment bug, now fixed: `normalized` drops blank entries but the list this was
    # zipped against (`extracted_skills`) does not, so a single whitespace-only skill shifted
    # every subsequent pairing by one -- "Java" would be compared against the normalized form
    # of a different skill entirely, and the last real skill dropped off the end. That made
    # duplicate detection both miss genuine duplicates and invent ones that were not there,
    # and the resulting count feeds a penalty on the student's ATS score. Deriving both values
    # in a single pass over the same filtered items makes the pairing correct by construction
    # rather than by two list comprehensions happening to agree.
    non_blank_skills = [s for s in extracted_skills if s.strip()]
    normalized = [_normalize(s) for s in non_blank_skills]
    seen: dict = {}
    duplicate_groups = 0
    for original, norm in zip(non_blank_skills, normalized, strict=True):
        if norm in seen and seen[norm].lower() != original.lower():
            duplicate_groups += 1
        seen[norm] = original
    if duplicate_groups > 0:
        score -= min(_DUPLICATE_PENALTY, duplicate_groups * 5)
        improvements.append("Some skills may be listed more than once under different names")
        issues.append(
            IssueDraft(
                category="skills",
                severity="low",
                title="Duplicate-looking skills",
                description="A few skills appear to be listed more than once under slightly "
                "different names (e.g. 'JS' and 'JavaScript').",
                how_to_fix="Pick one consistent name for each skill.",
                points_recoverable=min(_DUPLICATE_PENALTY, duplicate_groups * 5),
            )
        )

    normalized_set = set(normalized)
    lower_text = raw_text.lower()
    unlisted = [
        term for term in _COMMON_TECH_TERMS
        if re.search(r"\b" + re.escape(term) + r"\b", lower_text) and _normalize(term) not in normalized_set
    ]
    if unlisted:
        score -= _UNLISTED_PENALTY
        examples = ", ".join(t.title() for t in unlisted[:3])
        improvements.append(f"{examples} mentioned in your resume but not listed as a skill")
        issues.append(
            IssueDraft(
                category="skills",
                severity="medium",
                title="Skills mentioned but not listed",
                description=f"We noticed {examples} mentioned elsewhere on your resume, but not in your Skills section.",
                how_to_fix="Add every technology you've actually used to your Skills section so it's easy to scan.",
                points_recoverable=_UNLISTED_PENALTY,
            )
        )

    skills_section = split_into_sections(raw_text).get("skills")
    if skills_section and re.search(r"[A-Za-z ]+:\s*\S", skills_section):
        strengths.append("Skills are organized into clear categories")

    final_score = clamp_score(score)
    if final_score >= 90:
        summary = "Your skills section is strong and well-covered."
    elif final_score >= 70:
        summary = "Your skills section covers the basics, with room to be more complete."
    else:
        summary = "Your skills section needs more coverage to give recruiters and ATS systems a clear picture."

    return CategoryResult(
        category="skills",
        score=final_score,
        summary=summary,
        strengths=strengths,
        improvements=improvements,
        how_to_improve=[issue.how_to_fix for issue in issues],
        issues=issues,
    )
