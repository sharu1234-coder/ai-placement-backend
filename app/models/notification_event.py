import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class NotificationEvent(Base):
    """A server-detected state transition worth telling the student about -- mission
    completion, a WeaknessRecord advancing to IMPROVING/CLOSED, evidence reaching strong/
    verified, or a Career Twin readiness milestone. There is no backend push-send
    infrastructure yet (no device tokens, no Firebase Admin integration -- see the Phase 14
    scoping decision), so this table is the delivery mechanism itself: the Android app polls
    GET /notifications/pending on resume, posts/records each row locally, then acks it here.
    delivered_at is set once the client confirms receipt, so a transition is only ever
    surfaced once."""

    __tablename__ = "notification_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("student_profiles.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc), index=True
    )
    delivered_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
