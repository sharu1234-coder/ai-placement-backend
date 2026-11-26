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
from app.storage.erasure import ERASURE_NOT_IMPLEMENTED
from app.storage.provider import StorageError, StorageProvider

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "resume_parser_v2.txt"


class ResumeProcessingError(Exception):
    """Raised when the uploaded file can't be read at all (not a valid/readable PDF)."""


#: A deletion refused because the provider could not state where it would be deleting from.
#: Not an erasure failure reported by storage -- the destructive call was never made. It
#: matches `deletion_worker.IDENTITY_UNKNOWN` deliberately: the same refusal, reached the same
#: way, should read the same in both places.
ERASURE_IDENTITY_UNKNOWN = "StorageIdentityUnknown"


class ResumeErasureFailed(Exception):
    """The resume's file could not be *proved* erased, so nothing was deleted.

    Deliberately not a boolean. `delete_resume` used to swallow every storage failure and
    delete the database rows anyway, which destroyed the only remaining pointer to an object
    that was still stored -- the exact invariant the account-deletion outbox exists to hold.
    Raising forces the caller to answer for it, and the resume and its file are both still
    there when it does.

    `code` is the erasure contract's code (`app.storage.erasure`), so the caller classifies
    with the same sets the deletion worker uses rather than inventing a second taxonomy. It
    names a condition, never a bucket, a key or a credential.
    """

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


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
    the profile never claims a signal came from a resume that no longer exists.

    **Erasure comes first, and the rows only go once it is proved.** This used to call
    `storage.delete`, catch every exception, log a warning and delete the rows regardless.
    Three things were wrong with that, and Stage B made all three visible:

    `delete` is not an erasure. On a versioned bucket a delete without a version id inserts a
    delete marker; the object stops being listed, reads answer 404, and every byte stays
    stored. The student was told their resume was deleted and it was not. `erase` is the
    contract that actually promises it -- it enumerates the key's version history, deletes each
    version and marker by id, enumerates again, and raises rather than returning if anything is
    left. Account deletion has gone through it since Stage B; this was the last path that had
    not, so the two told the student different truths about the same word.

    Swallowing the failure destroyed the last pointer. With the row gone, nothing anywhere
    recorded that the object existed -- not the database, not an outbox job. It could never be
    found again, let alone erased. So a failure now raises `ResumeErasureFailed` and the row,
    the file path and the file all stay exactly where they are, which is the same invariant the
    account-deletion outbox holds by writing the key down before deleting anything.

    The order is now erase, then mutate, which is the opposite of before and is the only safe
    order here. The FK clean-up below is destructive in its own right: it deletes this resume's
    analyses and ATS checks. Doing that first and *then* discovering the file cannot be erased
    would leave a resume whose derived data had been thrown away. Erasing first also makes the
    crash window benign rather than fatal: if the process dies between the erasure and the
    commit, the row survives pointing at an object that is already gone, and the retry calls
    `erase` again -- which reports `already_absent` on an empty history and deletes no marker,
    so the retry succeeds and adds nothing. That only holds because `erase` is idempotent, and
    it is why this order is safe rather than merely tidier.

    A resume with no `file_path` has nothing stored, so there is nothing to erase and the rows
    go as before. That is a real state in this schema (the column is nullable), not a shortcut.
    """
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        # Ownership, unchanged and still first: a resume belonging to another profile is
        # reported as absent and never reaches the erasure below.
        return False

    if resume.file_path:
        _erase_stored_file(storage, resume.file_path, resume_id=resume.id)

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

    db.delete(resume)
    db.commit()
    return True


def _erase_stored_file(storage: StorageProvider, key: str, *, resume_id: int) -> None:
    """Permanently erase one stored resume file, or raise without having changed anything.

    Returns only on an `ErasureOutcome`, which the provider returns only after verifying that
    no version and no delete marker of the key remains. There is no partial success and no
    best-effort path: every other ending raises.

    Two refusals happen before the destructive call, and both fail closed:

    A provider with no `erase` is resolved with `getattr` rather than called directly, so a
    conforming-looking object that predates the erasure contract is refused instead of raising
    `AttributeError` from the middle of a request. Real adapters cannot reach this -- the base
    class supplies an `erase` that refuses -- but a duck-typed one can.

    A provider that cannot state its own destination does not get to perform a destructive
    operation. `StorageProvider.identity()` returns None exactly when the instance cannot say
    where it points, and the base class is explicit that such an instance "must not be treated
    as pointing at the right place". The deletion worker refuses the same condition under the
    same name. Note what is *not* checked here, and why: the worker compares a recorded identity
    against the live one because days can pass between enqueue and execution, and configuration
    can drift in between. Here the identity is read and acted on in the same breath, so there is
    no window to guard -- only the question of whether the provider can answer at all.

    Nothing in here logs the key. `resumes.file_path` is a user-scoped path, so the resume id
    and the erasure code carry the diagnosis instead, which is the same rule Stage A set for
    the worker's logs.
    """
    eraser = getattr(storage, "erase", None)
    if not callable(eraser):
        logger.error(
            "Storage provider %s has no version-aware erasure, so resume_id=%s was not deleted",
            type(storage).__name__,
            resume_id,
        )
        raise ResumeErasureFailed(
            f"Storage provider {type(storage).__name__} cannot prove an object has been erased",
            code=ERASURE_NOT_IMPLEMENTED,
        )

    identify = getattr(storage, "identity", None)
    if not callable(identify) or identify() is None:
        logger.error(
            "Storage provider %s cannot state its destination, so resume_id=%s was not deleted",
            type(storage).__name__,
            resume_id,
        )
        raise ResumeErasureFailed(
            f"Storage provider {type(storage).__name__} cannot state where it would delete from",
            code=ERASURE_IDENTITY_UNKNOWN,
        )

    try:
        outcome = eraser(key)
    except StorageError as exc:
        # The code, not the message: the message can name a bucket, the code never does.
        logger.warning(
            "Could not erase the file for resume_id=%s (%s); the resume was left in place",
            resume_id,
            exc.code or type(exc).__name__,
        )
        raise ResumeErasureFailed(
            f"The stored file for resume {resume_id} could not be erased", code=exc.code
        ) from exc

    logger.info(
        "Erased the file for resume_id=%s (versioning=%s versions=%s markers=%s verified_by=%s)",
        resume_id,
        outcome.versioning,
        outcome.versions_deleted,
        outcome.markers_deleted,
        outcome.verified_by,
    )
