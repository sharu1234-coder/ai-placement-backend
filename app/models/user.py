import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Embedded as the "tv" claim in every access/refresh token issued (app/core/security.py)
    # and checked against this column on every authenticated request (app/api/deps.py).
    # Bumping it (logout, password reset) makes every previously issued token for this user
    # fail that check immediately, without needing a token allowlist/denylist table -- the
    # tradeoff is coarse-grained (invalidates all of that user's sessions/devices at once,
    # not just one), which is acceptable since there's no concept of separate named sessions
    # anywhere else in this app yet.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
