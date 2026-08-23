import smtplib
from email.message import EmailMessage

from app.core.config import settings
from app.email.provider import EmailError, EmailProvider


class SMTPEmailProvider(EmailProvider):
    """Production email delivery (EMAIL_PROVIDER=smtp) via plain SMTP -- works with any
    SMTP-speaking provider (Gmail app passwords, SendGrid/SES/Postmark/Mailgun SMTP relays,
    a self-hosted relay, ...) since SMTP is a universal standard, the same reasoning
    S3StorageProvider uses for being S3-compatible rather than locked to one vendor. No
    production email vendor has been chosen yet; this class only needs valid SMTP
    credentials to work with whichever one is picked later."""

    def __init__(self) -> None:
        if not settings.smtp_host or not settings.smtp_from_address:
            raise EmailError("SMTP_HOST and SMTP_FROM_ADDRESS are required when EMAIL_PROVIDER=smtp.")

    def send_password_reset_email(self, to_email: str, code: str) -> None:
        message = EmailMessage()
        message["Subject"] = "Your AI Placement OS password reset code"
        message["From"] = settings.smtp_from_address
        message["To"] = to_email
        message.set_content(
            f"Your password reset code is: {code}\n\n"
            f"This code expires in {settings.password_reset_token_expire_minutes} minutes. "
            "If you didn't request a password reset, you can safely ignore this email."
        )
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                smtp.starttls()
                if settings.smtp_username and settings.smtp_password:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailError(f"Failed to send password reset email: {exc}") from exc
