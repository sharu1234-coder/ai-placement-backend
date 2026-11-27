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
from app.storage.identity import (
    IDENTITY_INCOMPLETE,
    IDENTITY_UNDETERMINABLE,
    StorageIdentity,
    destination_refusal,
    recorded_identity,
)
from app.storage.provider import StorageError, StorageProvider

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "resume_parser_v2.txt"


class ResumeProcessingError(Exception):
    """Raised when the uploaded file can't be read at all (not a valid/readable PDF)."""


class ResumeStorageUnconfirmed(Exception):
    """The storage this request holds is not provably the storage that has the resume's file.

    Raised instead of doing the I/O, on every path that touches a resume's object -- upload,
    download, the ATS PDF read and deletion -- because the three ways this can go wrong are
    each worse than a refusal:

    * a **read** from the wrong destination finds nothing and reports a missing file, or, if a
      key ever collides, hands back an object that is not the one asked for;
    * an **erasure** against the wrong destination finds the key absent, and absence is
      idempotent success, so the rows are deleted while the object stays where it is;
    * an **upload** whose destination was never recorded leaves a row that no later request can
      safely act on, which is the legacy state this stage exists to stop creating more of.

    `code` is one of `app.storage.identity.IDENTITY_FAILURES`, the same taxonomy the deletion
    worker reports, so an operator reads one vocabulary across both. It names a condition and
    never a bucket, an endpoint, an account or a key.

    Every one of these is a fact about configuration rather than about the request, which is
    why callers answer 500 and not 503: nothing about it clears on its own.
    """

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


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


def capture_destination(storage: StorageProvider) -> StorageIdentity:
    """Where this provider is about to write, as a fact to persist beside the key.

    Read from the provider instance that will perform the `save`, not from settings. The two
    are normally the same, but only the instance can answer for itself, and a provider built
    from configuration that has since been re-read would make the recorded destination a claim
    about settings rather than about the object.

    Refuses rather than recording a partial answer. An identity that cannot be stated, or that
    does not say enough to find the object again (`s3` with no bucket), is worse than no
    identity at all: it looks captured, so every later request treats it as confirmable.
    """
    reader = getattr(storage, "identity", None)
    live = reader() if callable(reader) else None
    if live is None:
        raise ResumeStorageUnconfirmed(
            f"Storage provider {type(storage).__name__} cannot state where it stores objects",
            code=IDENTITY_UNDETERMINABLE,
        )
    if not live.is_complete:
        raise ResumeStorageUnconfirmed(
            f"Storage provider {type(storage).__name__} did not say enough about its "
            "destination to record it",
            code=IDENTITY_INCOMPLETE,
        )
    return live


def confirm_destination(resume: Resume, storage: StorageProvider, *, action: str) -> None:
    """Refuse unless this storage is provably the storage holding this resume's file.

    The single gate in front of every object operation on a resume. It compares the
    destination recorded when the file was written against the one the live provider states,
    using the same fail-closed rule as the deletion worker
    (`app.storage.identity.destination_refusal`) -- so a resume uploaded under one
    configuration cannot be read from, or erased from, another.

    A resume with no recorded destination is refused too. That is the whole point of not
    backfilling: the alternative is to assume current configuration, which is indistinguishable
    from a correct answer afterwards and is exactly how an object becomes permanently
    unreachable while we report it deleted. `scripts/review_resume_identity.py` lists these.

    `action` appears only in the log line, never in a response. Nothing here logs the key, the
    bucket or the endpoint: the resume id, the action and the refusal code carry the diagnosis,
    which is the rule Stage A set for the worker and Stage B.1 kept for this service.
    """
    refusal = destination_refusal(recorded_identity(resume), storage)
    if refusal is None:
        return
    logger.error(
        "Refusing to %s resume_id=%s: its storage destination is not confirmed (%s)",
        action,
        resume.id,
        refusal,
    )
    raise ResumeStorageUnconfirmed(
        f"The storage holding resume {resume.id} could not be confirmed", code=refusal
    )


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
    # Before the save, deliberately. If the provider cannot say where it writes, the honest
    # outcome is that nothing is stored -- an object on disk that no row can be proved to point
    # at is the orphan this stage exists to stop manufacturing.
    destination = capture_destination(storage)
    file_path = _save_upload(storage, profile_id, filename, file_bytes)

    resume = Resume(
        profile_id=profile_id,
        file_path=file_path,
        original_filename=filename,
        file_size_bytes=len(file_bytes),
        raw_text=raw_text,
        parse_status="processing",
        # The destination, written in the same INSERT as the key it belongs to, so a resume
        # cannot exist with a key whose destination was never recorded. The revision is also the
        # "captured" marker that `recorded_identity` reads -- a `local` resume has no bucket
        # either way, so without it there is no telling a pre-capture row from a post-capture
        # one.
        storage_provider=destination.provider,
        storage_bucket=destination.bucket,
        storage_endpoint=destination.endpoint,
        storage_region=destination.region,
        storage_expected_owner=destination.expected_owner,
        storage_config_revision=destination.revision,
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

    # Before the read, and after ownership. A load from the wrong destination is not a harmless
    # miss: the key is a random path, so it almost always 404s and looks like a lost file, but
    # the one case that does not is handing a student bytes that are not theirs.
    confirm_destination(resume, storage, action="download")

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

    The `if resume.file_path` guard is defensive, not a path: the column is NOT NULL, so there
    is normally always an object to erase. It is there so that an empty string -- which the
    database permits and which no key can be -- is not handed to a provider as a key to erase.
    """
    resume = db.get(Resume, resume_id)
    if resume is None or resume.profile_id != profile_id:
        # Ownership, unchanged and still first: a resume belonging to another profile is
        # reported as absent and never reaches the erasure below.
        return False

    if resume.file_path:
        # Before the erasure, so a wrong destination costs nothing. This is the case the Stage
        # B identity guard could not see: it compared the destination recorded on a *job*
        # against the live provider, which closes the window between a deletion request and the
        # worker running, but a resume's file was written long before either.
        confirm_destination(resume, storage, action="delete")
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

    One refusal happens before the destructive call, and it fails closed: a provider with no
    `erase` is resolved with `getattr` rather than called directly, so a conforming-looking
    object that predates the erasure contract is refused instead of raising `AttributeError`
    from the middle of a request. Real adapters cannot reach this -- the base class supplies an
    `erase` that refuses -- but a duck-typed one can.

    The destination check is no longer here. Stage B.1 checked only whether the provider could
    state an identity at all, and said so in this docstring on the grounds that "the identity is
    read and acted on in the same breath, so there is no window to guard". That was wrong about
    where the window is. The object was written at upload time, so the gap to guard runs from
    then to now, not from this line to the next -- which is what `confirm_destination` compares,
    at the one call site in `delete_resume` that knows which resume is being deleted. A
    provider that cannot state its destination is still refused, now as
    `IDENTITY_UNDETERMINABLE` rather than under a code of this module's own.

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
