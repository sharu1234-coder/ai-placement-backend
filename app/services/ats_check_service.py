import logging
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.resume_ats_check import GeneratedAtsCheckContent
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.resume_ats_check import ResumeAtsCheck
from app.schemas.resume_ats_check import (
    AtsCheckListItemRead,
    AtsCheckReportRead,
    CategoryBreakdownRead,
    IssueRead,
    RecommendationRead,
    SectionAnalysisRead,
)
from app.services.ats_scoring.config import CATEGORY_LABELS
from app.services.ats_scoring.engine import ScoringResult, score_resume
from app.storage.provider import StorageError, StorageProvider

logger = logging.getLogger(__name__)

# Safety cap, not a product pagination feature (disclosed gap -- see assessment_service.py's
# _MAX_HISTORY_ROWS for the full rationale): protects response size for a long-lived account.
_MAX_HISTORY_ROWS = 200

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "resume_ats_check_v1.txt"


def _finding_key(category: str, title: str) -> str:
    return f"{category}::{title}"


def _build_prompt(scoring: ScoringResult, target_role: Optional[str]) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    category_scores_str = ", ".join(
        f"{CATEGORY_LABELS.get(cat, cat)}: {score}" for cat, score in scoring.category_scores.items()
    )
    findings_lines = [
        f"- [{_finding_key(issue['category'], issue['title'])}] ({issue['severity']}) "
        f"{issue['title']}: {issue['description']}"
        for issue in scoring.issues
    ]
    findings_block = "\n".join(findings_lines) if findings_lines else "(no issues detected)"

    return template.format(
        overall_score=scoring.overall_score,
        score_band=scoring.score_category,
        category_scores=category_scores_str,
        target_role=target_role or "(none given)",
        findings_block=findings_block,
    )


def _apply_ai_overlay(scoring: ScoringResult, generated: GeneratedAtsCheckContent) -> None:
    """Mutates scoring.issues/recommendations in place, replacing template text only for
    findings whose finding_key the AI echoed back exactly -- any unrecognized key is
    silently dropped, template text stands. A code-level enforcement of "never fabricate a
    finding," not just a prompt instruction."""
    known_keys = {_finding_key(i["category"], i["title"]) for i in scoring.issues}
    overlays = {r.finding_key: r for r in generated.finding_rephrases if r.finding_key in known_keys}
    if not overlays:
        return

    for issue in scoring.issues:
        overlay = overlays.get(_finding_key(issue["category"], issue["title"]))
        if overlay:
            issue["description"] = overlay.rationale
            issue["how_to_fix"] = overlay.how_to_improve

    for rec in scoring.recommendations:
        overlay = overlays.get(_finding_key(rec["category"], rec["title"]))
        if overlay:
            rec["rationale"] = overlay.rationale


def generate_ats_check(
    db: Session,
    profile_id: int,
    resume_id: int,
    target_role: Optional[str],
    ai_provider: AIProvider,
    storage: StorageProvider,
) -> Optional[AtsCheckReportRead]:
    """Composes existing data (Resume.raw_text, the stored PDF, ResumeAnalysis's
    already-extracted lists) through the deterministic engine, then layers one AI call for
    explanation only -- mirrors jd_service.generate_preparation_plan's exact shape."""
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        return None

    analysis = db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume_id).first()

    pdf_bytes: Optional[bytes] = None
    try:
        pdf_bytes = storage.load(resume.file_path)
    except StorageError as exc:
        logger.warning("Could not load stored PDF for resume_id=%s: %s", resume_id, exc)

    scoring = score_resume(
        raw_text=resume.raw_text,
        pdf_bytes=pdf_bytes,
        extracted_skills=analysis.extracted_skills if analysis else [],
        extracted_education=analysis.extracted_education if analysis else [],
        extracted_experience=analysis.extracted_experience if analysis else [],
        extracted_projects=analysis.extracted_projects if analysis else [],
        extracted_certifications=analysis.extracted_certifications if analysis else [],
    )

    ai_summary = scoring.deterministic_summary
    try:
        prompt = _build_prompt(scoring, target_role)
        generated: GeneratedAtsCheckContent = ai_provider.generate_structured(prompt, GeneratedAtsCheckContent)
        ai_summary = generated.summary
        _apply_ai_overlay(scoring, generated)
    except AIGenerationError as exc:
        logger.warning("ATS check AI explanation failed for resume_id=%s: %s", resume_id, exc)
        # never blocks the check from being created -- deterministic score/issues/
        # recommendations/template summary already stand on their own.

    check = ResumeAtsCheck(
        resume_id=resume_id,
        profile_id=profile_id,
        target_role=target_role,
        overall_score=scoring.overall_score,
        score_category=scoring.score_category,
        parsing_score=scoring.category_scores["parsing"],
        structure_score=scoring.category_scores["structure"],
        content_score=scoring.category_scores["content"],
        skills_score=scoring.category_scores["skills"],
        experience_score=scoring.category_scores["experience"],
        projects_score=scoring.category_scores["projects"],
        contact_score=scoring.category_scores["contact"],
        readability_score=scoring.category_scores["readability"],
        category_breakdown=scoring.category_breakdown,
        section_analysis=scoring.section_analysis,
        issues=scoring.issues,
        recommendations=scoring.recommendations,
        potential_score=scoring.potential_score,
        potential_score_explanation=scoring.potential_score_explanation,
        ai_summary=ai_summary,
    )
    db.add(check)
    db.commit()
    db.refresh(check)

    return _to_report_read(check, resume.raw_text)


def _to_report_read(check: ResumeAtsCheck, raw_text: str) -> AtsCheckReportRead:
    return AtsCheckReportRead(
        id=check.id,
        resume_id=check.resume_id,
        target_role=check.target_role,
        overall_score=check.overall_score,
        score_category=check.score_category,
        category_breakdown=[CategoryBreakdownRead(**c) for c in check.category_breakdown],
        section_analysis=[SectionAnalysisRead(**s) for s in check.section_analysis],
        issues=[IssueRead(**i) for i in check.issues],
        recommendations=[RecommendationRead(**r) for r in check.recommendations],
        potential_score=check.potential_score,
        potential_score_explanation=check.potential_score_explanation,
        ai_summary=check.ai_summary,
        raw_text=raw_text,
        created_at=check.created_at,
    )


def get_ats_check(db: Session, check_id: int, profile_id: int) -> Optional[AtsCheckReportRead]:
    check = db.get(ResumeAtsCheck, check_id)
    if check is None or check.profile_id != profile_id:
        return None
    resume = db.get(Resume, check.resume_id)
    return _to_report_read(check, resume.raw_text if resume else "")


def list_ats_checks(db: Session, profile_id: int) -> List[AtsCheckListItemRead]:
    rows = (
        db.query(ResumeAtsCheck, Resume.original_filename)
        .join(Resume, Resume.id == ResumeAtsCheck.resume_id)
        .filter(ResumeAtsCheck.profile_id == profile_id)
        .order_by(ResumeAtsCheck.id.desc())
        .limit(_MAX_HISTORY_ROWS)
        .all()
    )
    return [
        AtsCheckListItemRead(
            id=check.id,
            resume_id=check.resume_id,
            resume_filename=filename,
            overall_score=check.overall_score,
            score_category=check.score_category,
            created_at=check.created_at,
        )
        for check, filename in rows
    ]
