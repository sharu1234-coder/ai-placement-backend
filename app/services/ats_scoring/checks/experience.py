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
_WEAK_OPENER_PENALTY = 10
_SHORT_DESCRIPTION_PENALTY = 10

# ResumeExperience has no date fields at all (see app/ai/schemas/resume.py) -- so
# reverse-chronological/date-order checking is deliberately out of scope here rather than
# faked from raw text, which would be unreliable and dishonest.


def check_experience(extracted_experience: List[Dict[str, Any]]) -> CategoryResult:
    """Category E -- Experience Quality (15%). Reuses ResumeAnalysis's already-extracted
    experience entries. Zero entries gets a supportive, non-punitive baseline -- many
    students genuinely have no work experience yet, and their Projects section can carry
    more weight instead."""
    if not extracted_experience:
        return CategoryResult(
            category="experience",
            score=75,
            summary="You don't have work experience listed yet -- that's common for students. "
            "A strong Projects section can carry real weight here too.",
        )

    score = 100.0
    issues: List[IssueDraft] = []
    strengths: List[str] = []
    improvements: List[str] = []

    descriptions = [
        (entry.get("description") or "").strip() for entry in extracted_experience
    ]
    empty_count = sum(1 for d in descriptions if not d)
    if empty_count:
        score -= min(_EMPTY_DESCRIPTION_PENALTY, empty_count * 10)
        improvements.append(f"{empty_count} experience entr{'y' if empty_count == 1 else 'ies'} have no description")
        issues.append(
            IssueDraft(
                category="experience",
                severity="high",
                title="Missing experience descriptions",
                description=f"{empty_count} of your experience entries don't have any description.",
                how_to_fix="Add 1-3 bullet points to each role describing what you actually did.",
                points_recoverable=min(_EMPTY_DESCRIPTION_PENALTY, empty_count * 10),
            )
        )

    non_empty = [d for d in descriptions if d]
    if non_empty:
        short = sum(1 for d in non_empty if len(d.split()) < _MIN_DESCRIPTION_WORDS)
        if short:
            score -= _SHORT_DESCRIPTION_PENALTY
            improvements.append("Some experience descriptions are very brief")
            issues.append(
                IssueDraft(
                    category="experience",
                    severity="medium",
                    title="Very short experience descriptions",
                    description=f"{short} experience description(s) are quite brief.",
                    how_to_fix="Expand each one with what you did, how, and what the outcome was.",
                    points_recoverable=_SHORT_DESCRIPTION_PENALTY,
                )
            )

        quantified = sum(1 for d in non_empty if _QUANTIFIED_RE.search(d))
        if quantified / len(non_empty) < 0.34:
            score -= _LOW_QUANTIFICATION_PENALTY
            improvements.append("Experience bullets lack measurable impact")
            issues.append(
                IssueDraft(
                    category="experience",
                    severity="high",
                    title="Add measurable achievements to your experience",
                    description="Most of your experience entries don't show measurable impact.",
                    how_to_fix="Add a number, percentage, or concrete outcome to each experience bullet where truthful.",
                    points_recoverable=_LOW_QUANTIFICATION_PENALTY,
                )
            )
        else:
            strengths.append("Includes measurable impact in your experience")

        weak = sum(1 for d in non_empty if d.lower().startswith(_WEAK_OPENERS))
        if weak / len(non_empty) > 0.3:
            score -= _WEAK_OPENER_PENALTY
            improvements.append("Some experience bullets open with weak phrases")
        else:
            strengths.append("Uses action verbs to describe your work")

    if not empty_count:
        strengths.append("Every experience entry has a description")

    final_score = clamp_score(score)
    if final_score >= 90:
        summary = "Your experience section is strong, with clear, impact-focused descriptions."
    elif final_score >= 70:
        summary = "Your experience section is solid, with room to show more measurable impact."
    else:
        summary = "Your experience descriptions could do more to show what you actually accomplished."

    return CategoryResult(
        category="experience",
        score=final_score,
        summary=summary,
        strengths=strengths,
        improvements=improvements,
        how_to_improve=[issue.how_to_fix for issue in issues],
        issues=issues,
    )
