import logging

from app.email.provider import EmailProvider

logger = logging.getLogger(__name__)


class ConsoleEmailProvider(EmailProvider):
    """Dev default (EMAIL_PROVIDER=console) -- logs the reset code instead of sending a
    real email. This never claims to have delivered anything (unlike a fake integration
    that silently no-ops while looking real); it's an honest stand-in so local/dev password
    -reset testing works without real email credentials."""

    def send_password_reset_email(self, to_email: str, code: str) -> None:
        logger.info(
            "[console email provider -- no real email sent] Password reset code for %s: %s",
            to_email,
            code,
        )
