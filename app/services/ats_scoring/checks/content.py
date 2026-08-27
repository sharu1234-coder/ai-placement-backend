import re
from typing import List

from app.services.ats_scoring.section_split import split_into_sections
from app.services.ats_scoring.types import CategoryResult, IssueDraft, clamp_score

_BULLET_PREFIX_RE = re.compile(r"^\s*[-•*●▪◦‣]\s*")
_QUANTIFIED_RE = re.compile(r"\d|%|\$|₹")
_WEAK_OPENERS = (
    "responsible for", "worked on", "helped with", "duties included",
    "in charge of", "tasked with", "involved in", "assisted with",
)
_FILLER_PHRASES = (
    "team player", "hard worker", "detail-oriented", "detail oriented",
    "results-driven", "results driven", "self-motivated", "self motivated",
    "excellent communication skills", "go-getter", "think outside the box",
)

_MIN_BULLETS_FOR_RATIO_CHECKS = 3
_WEAK_OPENER_RATIO_THRESHOLD = 0.3
_QUANTIFIED_RATIO_THRESHOLD = 0.2
_FILLER_COUNT_THRESHOLD = 3
_LONG_BULLET_CHARS = 220
_MIN_SUMMARY_WORDS = 20

_WEAK_OPENER_PENALTY = 15
_LOW_QUANTIFICATION_PENALTY = 20
_FILLER_PENALTY = 10
_LONG_BULLET_PENALTY = 5
_GENERIC_SUMMARY_PENALTY = 10


_MIN_CANDIDATE_LINE_WORDS = 4


def _extract_bullets(raw_text: str) -> List[str]:
    bullets = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _BULLET_PREFIX_RE.match(stripped):
            bullets.append(_BULLET_PREFIX_RE.sub("", stripped).strip())
    if bullets:
        return bullets

    # PDF-to-text extraction sometimes drops literal bullet glyphs even when the resume
    # visually has bullets (font-encoding dependent) -- fall back to treating each
    # reasonably long line inside Experience/Projects as a candidate bullet, rather than
    # silently skipping content scoring on an otherwise well-written resume.
    sections = split_into_sections(raw_text)
    for key in ("experience", "projects"):
        for line in (sections.get(key) or "").splitlines():
            stripped = line.strip()
            if len(stripped.split()) >= _MIN_CANDIDATE_LINE_WORDS:
                bullets.append(stripped)
    return bullets


def check_content(raw_text: str) -> CategoryResult:
    """Category C -- Content Quality (15%). Operates on bullet-like lines across the whole
    document (extracted PDF text often keeps or drops literal bullet glyphs inconsistently,
    so this stays a best-effort text heuristic, not a claim of true NLP quality judgment --
    grammar/spelling and subjective "impressiveness" are deliberately out of scope)."""
    bullets = _extract_bullets(raw_text)
    score = 100.0
    issues: List[IssueDraft] = []
    strengths: List[str] = []
    improvements: List[str] = []

    if len(bullets) >= _MIN_BULLETS_FOR_RATIO_CHECKS:
        weak_count = sum(1 for b in bullets if b.lower().startswith(_WEAK_OPENERS))
        weak_ratio = weak_count / len(bullets)
        if weak_ratio > _WEAK_OPENER_RATIO_THRESHOLD:
            score -= _WEAK_OPENER_PENALTY
            improvements.append("Several bullets open with weak phrases like 'responsible for'")
            issues.append(
                IssueDraft(
                    category="content",
                    severity="medium",
                    title="Weak bullet openers",
                    description="Several of your bullet points start with passive phrases like "
                    "'Responsible for' or 'Worked on', which undersell what you actually did.",
                    how_to_fix="Start each bullet with a strong action verb -- Built, Led, Reduced, Designed, Automated.",
                    points_recoverable=_WEAK_OPENER_PENALTY,
                )
            )
        else:
            strengths.append("Uses action verbs")

        quantified_count = sum(1 for b in bullets if _QUANTIFIED_RE.search(b))
        quantified_ratio = quantified_count / len(bullets)
        if quantified_ratio < _QUANTIFIED_RATIO_THRESHOLD:
            score -= _LOW_QUANTIFICATION_PENALTY
            improvements.append("Most bullets lack measurable impact")
            issues.append(
                IssueDraft(
                    category="content",
                    severity="high",
                    title="Add measurable achievements",
                    description="Most of your experience bullets don't show measurable impact -- "
                    "recruiters and screening systems respond well to concrete evidence of results.",
                    how_to_fix="Add numbers wherever truthful -- a percentage, a team size, a time saved, "
                    "a count of users or records. Instead of 'Worked on a data analysis project', try "
                    "'Analyzed 50,000+ records and reduced reporting time by 30%'.",
                    points_recoverable=_LOW_QUANTIFICATION_PENALTY,
                )
            )
        else:
            strengths.append("Several bullets include measurable results")

        long_bullets = sum(1 for b in bullets if len(b) > _LONG_BULLET_CHARS)
        if long_bullets >= 2:
            score -= _LONG_BULLET_PENALTY
            improvements.append("Some bullets run very long")
            issues.append(
                IssueDraft(
                    category="content",
                    severity="low",
                    title="Some bullets are too long",
                    description=f"{long_bullets} of your bullets are quite long, which can bury the key point.",
                    how_to_fix="Aim for one clear idea per bullet, roughly 1-2 lines.",
                    points_recoverable=_LONG_BULLET_PENALTY,
                )
            )

    filler_count = sum(1 for phrase in _FILLER_PHRASES if phrase in raw_text.lower())
    if filler_count >= _FILLER_COUNT_THRESHOLD:
        score -= _FILLER_PENALTY
        improvements.append("Several generic buzzwords found")
        issues.append(
            IssueDraft(
                category="content",
                severity="low",
                title="Generic buzzwords",
                description="Phrases like 'team player' or 'hard worker' are common and don't tell "
                "a reader much on their own.",
                how_to_fix="Replace generic phrases with a specific example that demonstrates the trait.",
                points_recoverable=_FILLER_PENALTY,
            )
        )

    sections = split_into_sections(raw_text)
    summary = sections.get("summary")
    if summary is not None:
        summary_words = len(summary.split())
        summary_filler_hits = sum(1 for phrase in _FILLER_PHRASES if phrase in summary.lower())
        if summary_words < _MIN_SUMMARY_WORDS or (summary_filler_hits >= 2 and not _QUANTIFIED_RE.search(summary)):
            score -= _GENERIC_SUMMARY_PENALTY
            improvements.append("Summary is too generic")
            issues.append(
                IssueDraft(
                    category="content",
                    severity="high",
                    title="Summary is too generic",
                    description="Your summary doesn't clearly highlight your specific skills and value -- "
                    "as written, it could describe almost any candidate.",
                    how_to_fix="Write a short, role-focused summary naming your strongest concrete skills "
                    "and what you're aiming for.",
                    points_recoverable=_GENERIC_SUMMARY_PENALTY,
                )
            )

    final_score = clamp_score(score)
    if final_score >= 90:
        content_summary = "Your resume's content is specific and impact-focused."
    elif final_score >= 70:
        content_summary = "Your resume's content is solid, with a few opportunities to sound more specific and impactful."
    else:
        content_summary = "Your resume's wording could do more to show your specific impact -- a few fixes would help a lot."

    return CategoryResult(
        category="content",
        score=final_score,
        summary=content_summary,
        strengths=strengths,
        improvements=improvements,
        how_to_improve=[issue.how_to_fix for issue in issues],
        issues=issues,
    )
