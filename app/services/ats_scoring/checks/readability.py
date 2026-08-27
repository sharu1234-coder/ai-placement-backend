import re
from typing import Optional

from app.services.ats_scoring.layout import LayoutSignals
from app.services.ats_scoring.section_split import heading_lines
from app.services.ats_scoring.types import CategoryResult, IssueDraft, clamp_score

_EXCESS_BLANK_LINES_RE = re.compile(r"\n{4,}")
_MAX_BODY_FONT_SIZES = 3

_INCONSISTENT_HEADINGS_PENALTY = 10
_EXCESS_BLANK_LINES_PENALTY = 5
_SIZE_VARIANCE_PENALTY = 10


def _heading_style(line: str) -> str:
    if line.isupper():
        return "upper"
    if line.istitle():
        return "title"
    return "other"


def check_readability(raw_text: str, layout: Optional[LayoutSignals]) -> CategoryResult:
    """Category H -- Readability & Consistency (5%). Heading-capitalization consistency
    and blank-line spacing are raw-text-derivable (no dates needed, unlike experience
    chronology); font-size variance reuses category A's layout re-extraction for a
    different judgment. "Unexplained abbreviations" and tone/voice consistency are
    deliberately out of scope -- no reliable heuristic exists for either."""
    score = 100.0
    issues = []
    strengths = []
    improvements = []

    headings = heading_lines(raw_text)
    styles = {_heading_style(h) for h in headings if h}
    if len(headings) >= 3 and len(styles - {"other"}) > 1:
        score -= _INCONSISTENT_HEADINGS_PENALTY
        improvements.append("Section heading style is inconsistent")
        issues.append(
            IssueDraft(
                category="readability",
                severity="low",
                title="Inconsistent heading style",
                description="Your section headings mix different capitalization styles (e.g. "
                "ALL CAPS and Title Case).",
                how_to_fix="Pick one heading style and use it for every section.",
                points_recoverable=_INCONSISTENT_HEADINGS_PENALTY,
            )
        )
    elif headings:
        strengths.append("Section headings are consistently styled")

    if _EXCESS_BLANK_LINES_RE.search(raw_text):
        score -= _EXCESS_BLANK_LINES_PENALTY
        improvements.append("Large gaps of empty space detected")
        issues.append(
            IssueDraft(
                category="readability",
                severity="low",
                title="Uneven spacing",
                description="Your resume has some unusually large gaps of empty space.",
                how_to_fix="Keep spacing between sections consistent throughout.",
                points_recoverable=_EXCESS_BLANK_LINES_PENALTY,
            )
        )

    if layout is not None and layout.distinct_size_count > _MAX_BODY_FONT_SIZES:
        score -= _SIZE_VARIANCE_PENALTY
        improvements.append("Text size varies a lot across the document")
        issues.append(
            IssueDraft(
                category="readability",
                severity="low",
                title="Inconsistent text sizing",
                description=f"Your resume uses {layout.distinct_size_count} different text sizes, "
                "which can look inconsistent.",
                how_to_fix="Use one size for headings and one size for body text.",
                points_recoverable=_SIZE_VARIANCE_PENALTY,
            )
        )
    elif layout is not None:
        strengths.append("Text sizing is consistent")

    final_score = clamp_score(score)
    if final_score >= 90:
        summary = "Your resume is easy to scan and consistently formatted."
    elif final_score >= 70:
        summary = "Your resume is mostly easy to read, with a couple of small consistency issues."
    else:
        summary = "A few formatting inconsistencies make your resume harder to scan quickly."

    return CategoryResult(
        category="readability",
        score=final_score,
        summary=summary,
        strengths=strengths,
        improvements=improvements,
        how_to_improve=[issue.how_to_fix for issue in issues],
        issues=issues,
    )
