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
