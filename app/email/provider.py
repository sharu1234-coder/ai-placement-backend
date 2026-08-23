from abc import ABC, abstractmethod


class EmailError(Exception):
    """Raised when an email provider fails to send a message."""


class EmailProvider(ABC):
    """Provider-independent email interface. Services depend only on this, never a vendor
    SDK or smtplib directly."""

    @abstractmethod
    def send_password_reset_email(self, to_email: str, code: str) -> None:
        ...


def get_email_provider() -> EmailProvider:
    from app.core.config import settings

    if settings.email_provider == "console":
        from app.email.console_provider import ConsoleEmailProvider

        return ConsoleEmailProvider()

    if settings.email_provider == "smtp":
        from app.email.smtp_provider import SMTPEmailProvider

        return SMTPEmailProvider()

    raise NotImplementedError(f"Email provider '{settings.email_provider}' is not implemented yet")
