import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    # --- where that key actually lives ----------------------------------------------------
    # `file_path` is only meaningful to the storage that holds it, and `get_storage_provider()`
    # builds a provider from *current* settings on every request. Without these, reading or
    # deleting a resume uploaded months ago targets whatever bucket is configured now: the key
    # is absent there, a read fails for no apparent reason, and an erasure reports an
    # idempotent success having removed nothing while the original object sits in the old
    # bucket. AWS S3, Cloudflare R2, MinIO, Backblaze B2 and DigitalOcean Spaces are all `s3`
    # to this codebase, so the provider name cannot tell two destinations apart either.
    #
    # Same five columns as `deletion_jobs`, deliberately: one reader
    # (`app/storage/identity.py:recorded_identity`) is duck-typed over both, so "was an
    # identity captured" cannot be answered one way here and another way there.
    #
    # All nullable and never backfilled. A resume uploaded before these existed has no
    # recorded destination, and inventing one from current configuration is precisely the
    # failure this closes -- so those rows fail closed instead, and
    # `scripts/review_resume_identity.py` is how an operator finds them.
    #
    # No credential is stored. These say *where* an object is, never how to reach it.
    storage_provider: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    storage_bucket: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    storage_endpoint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    storage_region: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # The account that had to own the destination at upload time, or NULL when no owner guard
    # was configured. Bucket, endpoint and region do not pin an account: a bucket name can be
    # re-created under different ownership, and a multi-tenant endpoint can serve one name to
    # two tenants. An account id grants nothing, so it is not a credential -- but it names an
    # account, so it is never logged or reported.
    storage_expected_owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Short non-secret fingerprint of the four location fields, so an operator can group
    # resumes by configuration without a bucket name reaching a log. Also the "identity was
    # captured" marker: NULL means this resume predates capture.
    storage_config_revision: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # The student's original upload filename -- file_path is a randomized storage key
    # (see resume_service._save_upload), never fit to show in the UI. Nullable because
    # rows created before this column existed have no way to backfill it.
    original_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Same nullable-forever reasoning as original_filename -- populated only for uploads
    # going forward, never backfilled by re-reading every stored file.
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
