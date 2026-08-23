import datetime
import hashlib
import logging
import secrets

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.email.provider import EmailError, EmailProvider
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User

logger = logging.getLogger(__name__)

_CODE_BYTES = 4  # secrets.token_hex(4) -> 8 hex chars, ~4.3 billion combinations


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def request_password_reset(db: Session, email: str, email_provider: EmailProvider) -> None:
    """Always succeeds from the caller's point of view (the API route always returns the
    same generic message regardless of what happens here) whether or not the email is
    registered -- otherwise this endpoint would let an attacker enumerate which emails have
    accounts by checking which ones get a different response."""
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        return

    # Invalidate any previous unused codes for this user so only the newest one works.
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)
    ).delete(synchronize_session=False)

    code = secrets.token_hex(_CODE_BYTES)
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=settings.password_reset_token_expire_minutes
    )
    db.add(PasswordResetToken(user_id=user.id, token_hash=_hash_code(code), expires_at=expires_at))
    db.commit()

    try:
        email_provider.send_password_reset_email(user.email, code)
    except EmailError as exc:
        # Don't fail the request over a delivery failure -- the code still exists, and
        # revealing delivery success/failure here would leak the same enumeration signal
        # this function otherwise avoids. Logged so an operator can see delivery is broken.
        logger.error("Failed to send password reset email to user_id=%s: %s", user.id, exc)


def reset_password(db: Session, email: str, code: str, new_password: str) -> bool:
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        return False

    now = datetime.datetime.now(datetime.timezone.utc)
    token = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.token_hash == _hash_code(code),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .first()
    )
    if token is None:
        return False

    user.password_hash = hash_password(new_password)
    # Invalidates every access/refresh token already issued to this user (checked in
    # app/api/deps.get_current_user and POST /auth/refresh) -- otherwise a session started
    # before a password reset (e.g. by whoever the reset was protecting against) would stay
    # valid until its natural expiry regardless of the password change.
    user.token_version += 1
    token.used_at = now
    db.commit()
    return True
