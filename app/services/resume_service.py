import logging
import uuid
from pathlib import Path
from typing import Optional

import fitz
from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
from app.ai.output_guard import leaking_field
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.resume import ParsedResume
from app.models.job_search_profile import JobSearchProfile
from app.models.placement_path import PlacementPath
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.resume_ats_check import ResumeAtsCheck
from app.schemas.resume import ResumeAnalysisRead, ResumeSummaryRead, ResumeUploadResponse
from app.services.ai_boundary import outside_transaction
from app.storage.provider import StorageProvider

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "resume_parser_v2.txt"


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
    return render_prompt(template, resume_text=raw_text)


def _save_upload(storage: StorageProvider, profile_id: int, filename: str, file_bytes: bytes) -> str:
    suffix = Path(filename).suffix or ".pdf"
    key = f"resumes/{profile_id}_{uuid.uuid4().hex}{suffix}"
    return storage.save(key, file_bytes)


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
    storage: StorageProvider,
) -> ResumeUploadResponse:
    raw_text = extract_text_from_pdf(file_bytes)
    file_path = _save_upload(storage, profile_id, filename, file_bytes)

    resume = Resume(
        profile_id=profile_id,
        file_path=file_path,
        original_filename=filename,
        file_size_bytes=len(file_bytes),
        raw_text=raw_text,
        parse_status="processing",
    )
    db.add(resume)
    db.commit()
    # A plain int, taken before the boundary below closes the session: the Resume object itself
    # does not survive it, and the id is the only thing needed to find the row again.
    resume_id = resume.id

    parse_failure: Optional[AIGenerationError] = None
    parsed: Optional[ParsedResume] = None
    # Outside any transaction (F12-07). The "processing" row above is already committed, so there
    # is nothing pending to lose, and the parse -- 98-183s against a real model -- no longer holds
    # a pooled connection or a PostgreSQL snapshot while it runs.
    with outside_transaction(db):
        try:
            parsed = ai_provider.generate_structured(_build_prompt(raw_text), ParsedResume)
            # Checked before the ResumeAnalysis row exists, because a leak here does not stop at the
        # student's screen: extracted_skills is what skill_matching.latest_resume_skills reads,
        # which is what the Evidence Engine, Career Twin, Placement Path and every JD match are
        # built on. A leaked "skill" persisted here would be read back for months as something
        # the student claimed about themselves.
        #
            # Raised as a generation failure so it takes the route this service already has for
            # one: parse_status becomes "failed", no analysis row is written, and the student can
            # re-upload. The field path is safe to carry in the message -- it names which field
            # failed, never what it said.
            leaked = leaking_field(parsed)
            if leaked is not None:
                raise AIGenerationError(f"Resume parse output leaked prompt scaffolding in field {leaked}")
        except AIGenerationError as exc:
            # Recorded, not handled here: writing parse_status="failed" is a database write and
            # belongs on the other side of the boundary.
            parse_failure = exc

    # Back in a transaction, on freshly read state rather than the object from before the AI call.
    resume = db.get(Resume, resume_id)
    if resume is None:
        # The profile was deleted while the AI ran. Nothing to record against.
        raise AIGenerationError("The resume was removed while it was being parsed.")

    if parse_failure is not None:
        logger.warning("Resume parsing failed for resume_id=%s: %s", resume_id, parse_failure)
        resume.parse_status = "failed"
        db.commit()
        return ResumeUploadResponse(id=resume_id, parse_status="failed", analysis=None)

    assert parsed is not None  # no failure recorded, so the parse produced output
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


def read_uploaded_resume(db: Session, profile_id: int, resume_id: int) -> Optional[ResumeUploadResponse]:
    """An earlier upload as process_resume_upload would have returned it, or None.

    Used to answer a replayed upload from the Resume the first attempt created, so a lost
    response costs neither a second row nor a second parse-and-analyse pass. Re-read rather than
    cached, so the parse_status reported is whatever it is now. Ownership is re-checked here and
    never inferred from the claim.
    """
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        return None
    return ResumeUploadResponse(
        id=resume.id,
        parse_status=resume.parse_status,
        analysis=get_resume_analysis(db, resume.id, profile_id),
    )


def get_latest_resume(db: Session, profile_id: int) -> Optional[ResumeSummaryRead]:
    resume = (
        db.query(Resume)
        .filter(Resume.profile_id == profile_id)
        .order_by(Resume.id.desc())
        .first()
    )
    if resume is None:
        return None
    return ResumeSummaryRead(
        id=resume.id,
        filename=resume.original_filename,
        file_size_bytes=resume.file_size_bytes,
        parse_status=resume.parse_status,
        created_at=resume.created_at,
    )


def get_resume_analysis(db: Session, resume_id: int, profile_id: int) -> Optional[ResumeAnalysisRead]:
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        return None

    analysis = db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume_id).first()
    if analysis is None:
        return None

    return to_analysis_read(analysis)


class ResumeFileUnavailable(Exception):
    """The Resume row exists but its bytes could not be read back from storage."""


def get_resume_file(
    db: Session, resume_id: int, profile_id: int, storage: StorageProvider
) -> Optional[tuple[bytes, str]]:
    """Return (pdf_bytes, download_filename) for a resume the caller owns, else None.

    Ownership is checked the same way get_resume_analysis does -- the row is fetched by id and
    then rejected unless it belongs to this profile, so another student's resume_id is a plain
    404 rather than a 403 that would confirm the id exists.

    The storage key is never returned to the caller: it is an internal, randomized path, and
    handing it out would leak where uploads live. The client gets bytes and the student's own
    original filename, nothing else.
    """
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        return None

    try:
        data = storage.load(resume.file_path)
    except Exception as exc:  # storage backends raise their own error types (OSError, ClientError, ...)
        # Deliberately logs the resume id but not the storage key or the exception's own
        # message, which for S3 can embed the bucket and key.
        logger.warning("Resume file unreadable for resume_id=%s: %s", resume.id, type(exc).__name__)
        raise ResumeFileUnavailable from exc

    return data, resume.original_filename or "resume.pdf"


def delete_resume(db: Session, resume_id: int, profile_id: int, storage: StorageProvider) -> bool:
    """PROJECT_HANDOFF.md section 6: "Provide deletion controls for user data" /
    "Protect resumes and uploaded documents" -- there was previously no way to delete an
    uploaded resume at all. Removes the DB rows and the stored file itself.

    Must clear every other FK pointing at this resume first, or the final db.commit() below
    raises IntegrityError *after* the file is already gone -- the transaction rolls back so
    the Resume row survives, but its file_path now points at nothing, and every retry hits
    the identical error: a permanently undeletable resume. resume_ats_checks.resume_id is
    NOT NULL, so those rows must be deleted outright (mirrors account_service.delete_account's
    already-correct handling of this same table). placement_paths.source_resume_id is
    nullable and is only this resume's provenance, not the roadmap's data -- deleting a whole
    PlacementPath (and the topics/progress it carries) just because its source resume was
    cleaned up would be a worse bug than the one being fixed, so that FK is nulled out
    instead of the row being deleted.

    job_search_profiles.resume_id is a third FK into this table, added later by Job Hunt and
    missed here at the time. It is nulled for the same reason as PlacementPath: the student's
    Job Hunt setup -- target role, location, work mode, declared skills -- is their own data
    and has nothing to do with which resume it was once linked to. The `resume_skills`
    provenance key goes with it, exactly as update_search_profile(clear_resume=True) does, so
    the profile never claims a signal came from a resume that no longer exists."""
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        return False

    db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume.id).delete()
    db.query(ResumeAtsCheck).filter(ResumeAtsCheck.resume_id == resume.id).delete()
    db.query(PlacementPath).filter(PlacementPath.source_resume_id == resume.id).update(
        {PlacementPath.source_resume_id: None}
    )
    for search_profile in (
        db.query(JobSearchProfile).filter(JobSearchProfile.resume_id == resume.id).all()
    ):
        search_profile.resume_id = None
        provenance = dict(search_profile.signal_provenance or {})
        provenance.pop("resume_skills", None)
        search_profile.signal_provenance = provenance

    try:
        storage.delete(resume.file_path)
    except Exception as exc:  # best-effort: a stuck file must never block the DB delete
        logger.warning("Could not delete resume file %s for resume_id=%s: %s", resume.file_path, resume.id, exc)

    db.delete(resume)
    db.commit()
    return True
