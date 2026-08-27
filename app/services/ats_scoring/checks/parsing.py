from typing import Optional

from app.services.ats_scoring.layout import LayoutSignals
from app.services.ats_scoring.types import CategoryResult, IssueDraft, clamp_score

_MULTI_COLUMN_PENALTY = 20
_TABLE_PENALTY = 15
_FONT_COUNT_PENALTY = 10
_SMALL_TEXT_PENALTY = 10
_IMAGE_PENALTY = 5
_MAX_FONT_COUNT = 3
_MAX_SMALL_TEXT_RATIO = 0.15


def check_parsing(layout: Optional[LayoutSignals]) -> CategoryResult:
    """Category A -- ATS Parsing & Formatting (20%). Needs the re-opened PDF's layout data
    (multi-column, tables, fonts, small text, images) -- none of this is answerable from
    plain extracted text alone. Degrades to a neutral, unpenalized score when layout is
    unavailable (e.g. storage.load() failed) rather than guessing."""
    if layout is None:
        return CategoryResult(
            category="parsing",
            score=90,
            summary="We could read your resume's text, which is the most basic ATS requirement, "
            "but couldn't fully analyze its visual layout this time.",
            strengths=["Your resume text extracted cleanly"],
        )

    issues = []
    strengths = []
    improvements = []
    score = 100.0

    if layout.is_multi_column:
        score -= _MULTI_COLUMN_PENALTY
        improvements.append("Multiple columns detected")
        issues.append(
            IssueDraft(
                category="parsing",
                severity="high",
                title="Multi-column layout detected",
                description="Multi-column layouts can confuse ATS parsers, which often read "
                "left-to-right across the whole page and scramble the intended reading order.",
                how_to_fix="Use a single-column layout so every section reads top to bottom in the order you intend.",
                points_recoverable=_MULTI_COLUMN_PENALTY,
            )
        )
    else:
        strengths.append("Simple, single-column layout detected")

    if layout.has_tables:
        score -= _TABLE_PENALTY
        improvements.append("Tables may not parse correctly")
        issues.append(
            IssueDraft(
                category="parsing",
                severity="high",
                title="Table detected",
                description="Tables are frequently misread or skipped by ATS parsers, which can "
                "drop the content inside them entirely.",
                how_to_fix="Replace tables with plain text lines or bullet points.",
                points_recoverable=_TABLE_PENALTY,
            )
        )

    if layout.distinct_font_count > _MAX_FONT_COUNT:
        score -= _FONT_COUNT_PENALTY
        improvements.append(f"{layout.distinct_font_count} different fonts found")
        issues.append(
            IssueDraft(
                category="parsing",
                severity="medium",
                title="Inconsistent fonts",
                description=f"Your resume uses {layout.distinct_font_count} different fonts. "
                "Too many fonts can look inconsistent and occasionally cause rendering issues.",
                how_to_fix="Stick to one or two standard fonts throughout your resume.",
                points_recoverable=_FONT_COUNT_PENALTY,
            )
        )
    else:
        strengths.append("Standard fonts are used")

    if layout.small_text_ratio > _MAX_SMALL_TEXT_RATIO:
        score -= _SMALL_TEXT_PENALTY
        improvements.append("Some text may be too small to read reliably")
        issues.append(
            IssueDraft(
                category="parsing",
                severity="medium",
                title="Very small text detected",
                description="Some text on your resume is smaller than 8pt, which can be hard for "
                "both ATS systems and human reviewers to read reliably.",
                how_to_fix="Keep body text at 10pt or larger.",
                points_recoverable=_SMALL_TEXT_PENALTY,
            )
        )

    if layout.has_images:
        score -= _IMAGE_PENALTY
        improvements.append("Some icons or images found")
        issues.append(
            IssueDraft(
                category="parsing",
                severity="low",
                title="Icons or images detected",
                description="Your resume contains embedded images or icons. If any of these hold "
                "important text (like a logo or QR code), an ATS won't be able to read it.",
                how_to_fix="Make sure no critical information is locked inside an image -- keep it as real, selectable text.",
                points_recoverable=_IMAGE_PENALTY,
            )
        )
    else:
        strengths.append("No embedded images that could hide important text")

    if layout.page_count > 2:
        improvements.append(f"Resume runs {layout.page_count} pages")

    final_score = clamp_score(score)
    if final_score >= 90:
        summary = "Your resume's layout is clean and highly compatible with ATS parsers."
    elif final_score >= 70:
        summary = "Your resume is mostly ATS-friendly but has a few formatting issues that may cause parsing problems."
    else:
        summary = "Your resume's formatting has several issues that could make it hard for ATS systems to read correctly."

    return CategoryResult(
        category="parsing",
        score=final_score,
        summary=summary,
        strengths=strengths,
        improvements=improvements,
        how_to_improve=[issue.how_to_fix for issue in issues],
        issues=issues,
    )
