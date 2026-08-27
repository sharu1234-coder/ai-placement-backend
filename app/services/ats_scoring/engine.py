from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.services.ats_scoring.checks.contact import check_contact
from app.services.ats_scoring.checks.content import check_content
from app.services.ats_scoring.checks.experience import check_experience
from app.services.ats_scoring.checks.parsing import check_parsing
from app.services.ats_scoring.checks.projects import check_projects
from app.services.ats_scoring.checks.readability import check_readability
from app.services.ats_scoring.checks.skills import check_skills
from app.services.ats_scoring.checks.structure import check_structure
from app.services.ats_scoring.config import CATEGORY_LABELS, CATEGORY_WEIGHTS, band_for_score
from app.services.ats_scoring.layout import extract_layout_signals
from app.services.ats_scoring.types import CategoryResult, IssueDraft, clamp_score

_HIGH_MEDIUM_SEVERITIES = {"high", "medium"}
_MAX_RECOMMENDATIONS = 10


@dataclass
class ScoringResult:
    overall_score: int
    score_category: str
    category_scores: Dict[str, int]
    category_breakdown: List[dict]
    section_analysis: List[dict]
    issues: List[dict]
    recommendations: List[dict]
    potential_score: Optional[int]
    potential_score_explanation: Optional[str]
    deterministic_summary: str


def score_resume(
    raw_text: str,
    pdf_bytes: Optional[bytes],
    extracted_skills: List[str],
    extracted_education: List[Dict[str, Any]],
    extracted_experience: List[Dict[str, Any]],
    extracted_projects: List[Dict[str, Any]],
    extracted_certifications: List[Dict[str, Any]],
) -> ScoringResult:
    """Pure function: same input -> same output, every time. Zero DB/AI/network access --
    that's what makes "same resume -> same score" mechanically true rather than merely
    asserted. target_role is deliberately NOT a parameter here -- it may only ever reach
    the AI explanation layer (see ats_check_service.py), never this deterministic score.

    raw_text is always available (guaranteed by Resume.raw_text being NOT NULL). pdf_bytes
    and the extracted_* lists are each independently optional in practice (storage.load()
    can fail; upload-time AI parsing can have failed) -- every check below has a graceful,
    non-crashing path for when its richer input is missing, so an owned resume_id always
    produces a full score.
    """
    layout = extract_layout_signals(pdf_bytes) if pdf_bytes is not None else None

    extracted_counts = {
        "education": len(extracted_education),
        "experience": len(extracted_experience),
        "projects": len(extracted_projects),
        "certifications": len(extracted_certifications),
    }

    structure_result, structure_findings = check_structure(raw_text, extracted_counts)
    contact_result, contact_finding = check_contact(raw_text)

    category_results: Dict[str, CategoryResult] = {
        "parsing": check_parsing(layout),
        "structure": structure_result,
        "content": check_content(raw_text),
        "skills": check_skills(extracted_skills, raw_text),
        "experience": check_experience(extracted_experience),
        "projects": check_projects(extracted_projects),
        "contact": contact_result,
        "readability": check_readability(raw_text, layout),
    }

    weighted_sum = sum(category_results[cat].score * weight for cat, weight in CATEGORY_WEIGHTS.items())
    overall_score = clamp_score(weighted_sum)
    band = band_for_score(overall_score)

    category_breakdown = [
        {
            "category": cat,
            "score": category_results[cat].score,
            "summary": category_results[cat].summary,
            "strengths": category_results[cat].strengths[:5],
            "improvements": category_results[cat].improvements[:5],
            "how_to_improve": category_results[cat].how_to_improve[:5],
        }
        for cat in CATEGORY_WEIGHTS  # dict preserves A-H insertion order
    ]

    section_analysis = [
        {"section": f.section, "status": f.status, "content": f.content}
        for f in (structure_findings + [contact_finding])
    ]

    all_issues: List[IssueDraft] = [issue for result in category_results.values() for issue in result.issues]
    issues = [
        {
            "category": i.category,
            "severity": i.severity,
            "title": i.title,
            "description": i.description,
            "how_to_fix": i.how_to_fix,
        }
        for i in all_issues
    ]

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    ranked_issues = sorted(all_issues, key=lambda i: (severity_rank.get(i.severity, 3), -i.points_recoverable))
    recommendations = [
        {
            "rank": rank,
            "category": issue.category,
            "title": issue.title,
            "priority": issue.severity,
            "rationale": issue.description,
            "fix_target": "resume_upload" if issue.severity in _HIGH_MEDIUM_SEVERITIES else None,
        }
        for rank, issue in enumerate(ranked_issues[:_MAX_RECOMMENDATIONS], start=1)
    ]

    # points_recoverable are category-scale (0-100) points, not overall-scale -- scale each
    # by that category's weight before adding to the overall score, so a 20-point fix in a
    # 5%-weight category can't swing the overall number as much as one in a 20%-weight
    # category would.
    weighted_recoverable = sum(
        issue.points_recoverable * CATEGORY_WEIGHTS.get(issue.category, 0)
        for issue in all_issues
        if issue.severity in _HIGH_MEDIUM_SEVERITIES
    )
    if weighted_recoverable > 0:
        potential_score: Optional[int] = clamp_score(overall_score + weighted_recoverable)
        potential_score_explanation: Optional[str] = (
            f"Addressing your highest-impact issues could raise your estimated ATS readiness to "
            f"around {potential_score}/100. This is an estimate, not a guarantee."
        )
    else:
        potential_score = None
        potential_score_explanation = None

    strongest = max(category_results.values(), key=lambda r: r.score)
    weakest = min(category_results.values(), key=lambda r: r.score)
    if overall_score >= 80:
        deterministic_summary = (
            f"Your resume scores {overall_score}/100 ({band.label}). "
            f"{CATEGORY_LABELS[strongest.category]} is a particular strength."
        )
    else:
        deterministic_summary = (
            f"Your resume scores {overall_score}/100 ({band.label}). "
            f"{CATEGORY_LABELS[weakest.category]} is the area with the most room to improve."
        )

    return ScoringResult(
        overall_score=overall_score,
        score_category=band.key,
        category_scores={cat: category_results[cat].score for cat in CATEGORY_WEIGHTS},
        category_breakdown=category_breakdown,
        section_analysis=section_analysis,
        issues=issues,
        recommendations=recommendations,
        potential_score=potential_score,
        potential_score_explanation=potential_score_explanation,
        deterministic_summary=deterministic_summary,
    )
