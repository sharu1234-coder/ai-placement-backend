import smtplib
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.email.console_provider import ConsoleEmailProvider
from app.email.provider import EmailError
from app.email.smtp_provider import SMTPEmailProvider


def test_console_provider_logs_the_code(caplog):
    provider = ConsoleEmailProvider()
    with caplog.at_level("INFO"):
        provider.send_password_reset_email("student@example.com", "deadbeef")

    assert "student@example.com" in caplog.text
    assert "deadbeef" in caplog.text


def test_smtp_provider_requires_host_and_from(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "smtp_from_address", None)

    with pytest.raises(EmailError, match="SMTP_HOST"):
        SMTPEmailProvider()


@pytest.fixture()
def smtp_settings(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_from_address", "noreply@sgldquantlabs.com")
    monkeypatch.setattr(settings, "smtp_username", "user")
    monkeypatch.setattr(settings, "smtp_password", "pass")


def test_smtp_provider_sends_via_starttls_and_login(smtp_settings, monkeypatch):
    mock_smtp_instance = MagicMock()
    mock_smtp_instance.__enter__.return_value = mock_smtp_instance
    mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
    monkeypatch.setattr(smtplib, "SMTP", mock_smtp_cls)

    provider = SMTPEmailProvider()
    provider.send_password_reset_email("student@example.com", "deadbeef")

    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=10)
    mock_smtp_instance.starttls.assert_called_once()
    mock_smtp_instance.login.assert_called_once_with("user", "pass")
    mock_smtp_instance.send_message.assert_called_once()
    sent_message = mock_smtp_instance.send_message.call_args[0][0]
    assert sent_message["To"] == "student@example.com"
    assert "deadbeef" in sent_message.get_content()


def test_smtp_provider_raises_email_error_on_failure(smtp_settings, monkeypatch):
    mock_smtp_cls = MagicMock(side_effect=smtplib.SMTPException("boom"))
    monkeypatch.setattr(smtplib, "SMTP", mock_smtp_cls)

    provider = SMTPEmailProvider()
    with pytest.raises(EmailError):
        provider.send_password_reset_email("student@example.com", "deadbeef")
