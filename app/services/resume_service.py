import logging
import uuid
from pathlib import Path
from typing import Optional

import fitz
from sqlalchemy.orm import Session

from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.resume import ParsedResume
from app.core.config import settings
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.schemas.resume import ResumeAnalysisRead, ResumeUploadResponse

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "resume_parser_v1.txt"


class ResumeProcessingError(Exception):
    """Raised when the uploaded file can't be read at all (not a valid/readable PDF)."""


def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
    except Exception as exc:
        raise ResumeProcessingError(f"Could not read PDF: {exc}") from exc

    if not text.strip():
        raise ResumeProcessingError(
            "No extractable text found in this PDF (it may be scanned/image-only)."
        )

    return text


def _build_prompt(raw_text: str) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(resume_text=raw_text)


def _save_upload(profile_id: int, filename: str, file_bytes: bytes) -> str:
    upload_dir = Path(settings.upload_dir) / "resumes"
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix or ".pdf"
    stored_name = f"{profile_id}_{uuid.uuid4().hex}{suffix}"
    path = upload_dir / stored_name
    path.write_bytes(file_bytes)
    return str(path)


def to_analysis_read(analysis: ResumeAnalysis) -> ResumeAnalysisRead:
    return ResumeAnalysisRead(
        id=analysis.id,
        resume_id=analysis.resume_id,
        skills=analysis.extracted_skills,
        education=analysis.extracted_education,
        experience=analysis.extracted_experience,
        projects=analysis.extracted_projects,
        certifications=analysis.extracted_certifications,
        ats_score=analysis.ats_score,
        weak_sections=analysis.weak_sections,
        extraction_confidence=analysis.extraction_confidence,
    )


def process_resume_upload(
    db: Session,
    profile_id: int,
    filename: str,
    file_bytes: bytes,
    ai_provider: AIProvider,
) -> ResumeUploadResponse:
    raw_text = extract_text_from_pdf(file_bytes)
    file_path = _save_upload(profile_id, filename, file_bytes)

    resume = Resume(
        profile_id=profile_id, file_path=file_path, raw_text=raw_text, parse_status="processing"
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    try:
        parsed: ParsedResume = ai_provider.generate_structured(_build_prompt(raw_text), ParsedResume)
    except AIGenerationError as exc:
        logger.warning("Resume parsing failed for resume_id=%s: %s", resume.id, exc)
        resume.parse_status = "failed"
        db.commit()
        return ResumeUploadResponse(id=resume.id, parse_status=resume.parse_status, analysis=None)

    analysis = ResumeAnalysis(
        resume_id=resume.id,
        extracted_skills=parsed.skills,
        extracted_education=[item.model_dump() for item in parsed.education],
        extracted_experience=[item.model_dump() for item in parsed.experience],
        extracted_projects=[item.model_dump() for item in parsed.projects],
        extracted_certifications=[item.model_dump() for item in parsed.certifications],
        ats_score=parsed.ats_score,
        weak_sections=parsed.weak_sections,
        extraction_confidence=parsed.extraction_confidence,
    )
    db.add(analysis)
    resume.parse_status = "completed"
    db.commit()
    db.refresh(analysis)

    return ResumeUploadResponse(
        id=resume.id, parse_status=resume.parse_status, analysis=to_analysis_read(analysis)
    )


def get_resume_analysis(db: Session, resume_id: int, profile_id: int) -> Optional[ResumeAnalysisRead]:
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        return None

    analysis = db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume_id).first()
    if analysis is None:
        return None

    return to_analysis_read(analysis)


def delete_resume(db: Session, resume_id: int, profile_id: int) -> bool:
    """PROJECT_HANDOFF.md section 6: "Provide deletion controls for user data" /
    "Protect resumes and uploaded documents" -- there was previously no way to delete an
    uploaded resume at all. Removes the DB rows and the stored file itself."""
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        return False

    db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume.id).delete()

    try:
        Path(resume.file_path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete resume file %s for resume_id=%s", resume.file_path, resume.id)

    db.delete(resume)
    db.commit()
    return True
