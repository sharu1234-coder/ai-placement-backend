from typing import Optional

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class InterviewAnswer(Base):
    __tablename__ = "interview_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_question_id: Mapped[int] = mapped_column(
        ForeignKey("interview_questions.id"), nullable=False, unique=True, index=True
    )
    answer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    audio_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    transcribed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
