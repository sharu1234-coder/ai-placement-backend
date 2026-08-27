import re
from typing import Tuple

from app.services.ats_scoring.types import CategoryResult, IssueDraft, SectionFinding, clamp_score

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(\+?\d{1,3}[\s-]?)?(\(?\d{3,5}\)?[\s-]?){2,3}\d{3,4}")
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/[A-Za-z0-9\-_/]+", re.IGNORECASE)
_GITHUB_RE = re.compile(r"github\.com/[A-Za-z0-9\-_/]+", re.IGNORECASE)

_MISSING_EMAIL_PENALTY = 30
_MISSING_PHONE_PENALTY = 25
_MISSING_LINKEDIN_PENALTY = 15


def check_contact(raw_text: str) -> Tuple[CategoryResult, SectionFinding]:
    """Category G -- Contact/Professional Info (5%). Regex presence+format checks only --
    there's no way to verify the contact info actually belongs to the student, and that's
    explicitly out of scope."""
    score = 100.0
    issues = []
    strengths = []
    improvements = []

    has_email = bool(_EMAIL_RE.search(raw_text))
    has_phone = bool(_PHONE_RE.search(raw_text))
    has_linkedin = bool(_LINKEDIN_RE.search(raw_text))
    has_github = bool(_GITHUB_RE.search(raw_text))

    if has_email:
        strengths.append("Email address detected")
    else:
        score -= _MISSING_EMAIL_PENALTY
        improvements.append("No email address detected")
        issues.append(
            IssueDraft(
                category="contact",
                severity="high",
                title="Missing email address",
                description="We couldn't detect an email address on your resume.",
                how_to_fix="Add a professional email address near the top of your resume.",
                points_recoverable=_MISSING_EMAIL_PENALTY,
            )
        )

    if has_phone:
        strengths.append("Phone number detected")
    else:
        score -= _MISSING_PHONE_PENALTY
        improvements.append("No phone number detected")
        issues.append(
            IssueDraft(
                category="contact",
                severity="high",
                title="Missing phone number",
                description="We couldn't detect a phone number on your resume.",
                how_to_fix="Add a reachable phone number near the top of your resume.",
                points_recoverable=_MISSING_PHONE_PENALTY,
            )
        )

    if has_linkedin:
        strengths.append("LinkedIn profile detected")
    else:
        score -= _MISSING_LINKEDIN_PENALTY
        improvements.append("No LinkedIn profile detected")
        issues.append(
            IssueDraft(
                category="contact",
                severity="medium",
                title="Missing LinkedIn profile",
                description="We couldn't detect a LinkedIn profile link on your resume.",
                how_to_fix="Add your LinkedIn profile URL so recruiters can learn more about you.",
                points_recoverable=_MISSING_LINKEDIN_PENALTY,
            )
        )

    if has_github:
        strengths.append("GitHub profile detected")

    final_score = clamp_score(score)
    if final_score >= 90:
        summary = "Your contact information is complete and easy to find."
    elif final_score >= 70:
        summary = "Your contact information is mostly complete, with one or two details missing."
    else:
        summary = "Some important contact details are missing, which could make it hard for recruiters to reach you."

    category_result = CategoryResult(
        category="contact",
        score=final_score,
        summary=summary,
        strengths=strengths,
        improvements=improvements,
        how_to_improve=[issue.how_to_fix for issue in issues],
        issues=issues,
    )

    found_lines = []
    for pattern in (_EMAIL_RE, _PHONE_RE, _LINKEDIN_RE, _GITHUB_RE):
        match = pattern.search(raw_text)
        if match:
            found_lines.append(match.group(0))
    detected_count = sum([has_email, has_phone, has_linkedin])
    if detected_count == 3:
        status = "complete"
    elif detected_count > 0:
        status = "needs_improvement"
    else:
        status = "missing"
    section_finding = SectionFinding(
        section="contact", status=status, content="\n".join(found_lines) or None
    )

    return category_result, section_finding
