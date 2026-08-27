from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PlacementAlarmQuestionAttempt(Base):
    """One row per question slot (0..5) in a challenge. Unlike QuestionAttempt (regular
    assessments), which rejects re-answering a question once student_answer is set, this row
    stays re-answerable until is_correct flips True -- the alarm's product rule is "retry the
    same question until correct," not "one attempt per question." attempt_count/
    last_student_answer track the retry history without needing a separate attempts table."""

    __tablename__ = "placement_alarm_question_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    challenge_id: Mapped[int] = mapped_column(
        ForeignKey("placement_alarm_challenges.id"), nullable=False, index=True
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), nullable=False, index=True)
    question_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_student_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
