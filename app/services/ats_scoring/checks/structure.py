from typing import Dict, List, Tuple

from app.services.ats_scoring.section_split import split_into_sections
from app.services.ats_scoring.types import CategoryResult, IssueDraft, SectionFinding, clamp_score

_MIN_SUBSTANTIVE_WORDS = 15

# category key -> (deduction if missing, severity, is-gently-optional)
# "Gently optional" sections (experience) never produce harsh copy -- many students
# genuinely have none yet, and the spec explicitly requires supportive language.
_CORE_SECTIONS = {
    "skills": (25, "high", False),
    "education": (20, "high", False),
    "summary": (10, "medium", False),
    "experience": (5, "low", True),
}
_BONUS_SECTIONS = {
    "projects": (10, "medium", False),
}
_NICE_TO_HAVE_SECTIONS = ["certifications", "achievements", "languages"]
_NICE_TO_HAVE_MISSING_PENALTY = 5

_THIN_SECTION_PENALTY = 5


def check_structure(raw_text: str, extracted_counts: Dict[str, int]) -> Tuple[CategoryResult, List[SectionFinding]]:
    """Category B -- Resume Structure & Sections (15%). Detects sections by heading regex
    (section_split.py), cross-checked against extracted_counts (item counts already parsed
    at upload time by ResumeAnalysis) to catch "heading exists but nothing meaningful is
    under it" -- a real signal plain heading-presence alone would miss."""
    sections = split_into_sections(raw_text)
    score = 100.0
    issues: List[IssueDraft] = []
    strengths: List[str] = []
    improvements: List[str] = []
    findings: List[SectionFinding] = []

    for key, (penalty, severity, gentle) in {**_CORE_SECTIONS, **_BONUS_SECTIONS}.items():
        body = sections.get(key)
        extracted_count = extracted_counts.get(key, 0)

        if body is None:
            if gentle:
                # No harsh copy for a gently-optional section (experience) -- many students
                # genuinely have none yet. Still a real, if small, deduction.
                findings.append(SectionFinding(key, "missing", None))
                score -= penalty
                continue
            score -= penalty
            improvements.append(f"Missing {key.capitalize()} section")
            findings.append(SectionFinding(key, "missing", None))
            issues.append(
                IssueDraft(
                    category="structure",
                    severity=severity,
                    title=f"Missing {key.capitalize()} section",
                    description=f"We couldn't clearly detect a {key} section on your resume.",
                    how_to_fix=f"Try adding a simple heading such as '{key.capitalize()}' above that content.",
                    points_recoverable=penalty,
                )
            )
            continue

        word_count = len(body.split())
        is_thin = word_count < _MIN_SUBSTANTIVE_WORDS and extracted_count == 0
        if is_thin:
            score -= _THIN_SECTION_PENALTY
            improvements.append(f"{key.capitalize()} section looks thin")
            findings.append(SectionFinding(key, "needs_improvement", body[:1500] or None))
            issues.append(
                IssueDraft(
                    category="structure",
                    severity="low",
                    title=f"{key.capitalize()} section is thin",
                    description=f"We found a {key} heading, but there isn't much content under it.",
                    how_to_fix=f"Add a bit more detail to your {key} section.",
                    points_recoverable=_THIN_SECTION_PENALTY,
                )
            )
        else:
            status = "good" if key in _BONUS_SECTIONS else "complete"
            findings.append(SectionFinding(key, status, body[:1500] or None))
            strengths.append(f"Clear {key.capitalize()} section found")

    missing_nice_to_have = [k for k in _NICE_TO_HAVE_SECTIONS if k not in sections]
    for key in _NICE_TO_HAVE_SECTIONS:
        body = sections.get(key)
        if body is None:
            findings.append(SectionFinding(key, "missing", None))
        else:
            findings.append(SectionFinding(key, "good", body[:1500] or None))
    if len(missing_nice_to_have) == len(_NICE_TO_HAVE_SECTIONS):
        score -= _NICE_TO_HAVE_MISSING_PENALTY
        improvements.append("No certifications, achievements, or languages listed")

    final_score = clamp_score(score)
    if final_score >= 90:
        summary = "Your resume has a clear, complete section structure."
    elif final_score >= 70:
        summary = "Your resume's structure is mostly solid, with a few sections worth strengthening."
    else:
        summary = "A few important sections are missing or thin, which may make your resume harder to evaluate."

    result = CategoryResult(
        category="structure",
        score=final_score,
        summary=summary,
        strengths=strengths,
        improvements=improvements,
        how_to_improve=[issue.how_to_fix for issue in issues],
        issues=issues,
    )
    return result, findings
