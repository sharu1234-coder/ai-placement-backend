from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class RecruiterAttackTurn(Base):
    """One question+answer pair within a RecruiterAttackSession. answer_text stays None until
    answered -- recruiter_attack_service.get_next_turn (mirrors interview_service's
    get_next_question) reports the first turn with answer_text is None as pending, and
    generates a new one only once every existing turn has been answered."""

    __tablename__ = "recruiter_attack_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("recruiter_attack_sessions.id"), nullable=False, index=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Short tag describing what this question targets, e.g. "Your specific role", "Scaling claim".
    based_on: Mapped[str] = mapped_column(String(255), nullable=False)
    answer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
