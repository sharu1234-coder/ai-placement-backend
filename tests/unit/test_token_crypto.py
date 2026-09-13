"""Encryption at rest for provider credentials, and the ways it is allowed to fail.

The property worth stating plainly: what protects a leaked database backup is that the
ciphertext is in the dump and the key is in the process environment. So the test that matters
most here is not the round-trip -- it is that the plaintext token cannot be found anywhere in
what gets written to the database row, and that no error path puts a credential into an
exception message that will end up in a log aggregator.
"""

import pytest
from cryptography.fernet import Fernet

from app.core import token_crypto
from app.core.config import settings

_TOKEN = "gho_ThisIsNotARealGitHubTokenJustATestValue"


@pytest.fixture()
def configured_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "token_encryption_key", key)
    return key


class TestRoundTrip:
    def test_a_token_survives_encrypt_then_decrypt(self, configured_key):
        assert token_crypto.decrypt(token_crypto.encrypt(_TOKEN)) == _TOKEN

    def test_the_ciphertext_does_not_contain_the_token(self, configured_key):
        """The whole point. A stored value that still contains a recognisable fragment of the
        credential would defeat the purpose of storing it encrypted at all."""
        ciphertext = token_crypto.encrypt(_TOKEN)
        assert _TOKEN not in ciphertext
        assert "gho_" not in ciphertext

    def test_encrypting_the_same_token_twice_gives_different_ciphertext(self, configured_key):
        """Fernet generates a fresh IV per message. Identical ciphertexts would mean two
        students with the same token were visibly identical in a database dump, and would be a
        sign the construction had been replaced with something deterministic."""
        assert token_crypto.encrypt(_TOKEN) != token_crypto.encrypt(_TOKEN)

    def test_unicode_and_empty_values_survive(self, configured_key):
        for value in ("", "ünïcodé-tøken", "a" * 4096):
            assert token_crypto.decrypt(token_crypto.encrypt(value)) == value


class TestMisconfiguration:
    def test_encrypting_without_a_key_raises_rather_than_storing_plaintext(self, monkeypatch):
        """The failure mode this refuses is the quiet one: falling back to plaintext would mean
        a misconfigured deployment writing real GitHub tokens to the database in the clear
        while every endpoint kept returning 200."""
        monkeypatch.setattr(settings, "token_encryption_key", None)
        with pytest.raises(token_crypto.TokenEncryptionNotConfigured):
            token_crypto.encrypt(_TOKEN)

    def test_an_empty_key_counts_as_unset(self, monkeypatch):
        """`.env.example` ships TOKEN_ENCRYPTION_KEY= with no value, so a copied file produces
        an empty string rather than None. Both must be treated the same."""
        monkeypatch.setattr(settings, "token_encryption_key", "")
        assert token_crypto.is_configured() is False
        with pytest.raises(token_crypto.TokenEncryptionNotConfigured):
            token_crypto.encrypt(_TOKEN)

    def test_a_malformed_key_is_reported_as_not_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "token_encryption_key", "not-a-valid-fernet-key")
        assert token_crypto.is_configured() is False
        with pytest.raises(token_crypto.TokenEncryptionNotConfigured):
            token_crypto.encrypt(_TOKEN)

    def test_a_real_key_reports_configured(self, configured_key):
        assert token_crypto.is_configured() is True


class TestKeyRotation:
    def test_ciphertext_from_another_key_fails_closed(self, monkeypatch):
        """What a key rotation actually looks like from the application's side. The honest
        response is a specific error the caller can turn into "reconnect GitHub", not a crash
        and not silently treating the connection as absent."""
        first_key = Fernet.generate_key().decode()
        monkeypatch.setattr(settings, "token_encryption_key", first_key)
        ciphertext = token_crypto.encrypt(_TOKEN)

        monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
        with pytest.raises(token_crypto.TokenDecryptionFailed):
            token_crypto.decrypt(ciphertext)

    def test_corrupted_ciphertext_fails_closed(self, configured_key):
        with pytest.raises(token_crypto.TokenDecryptionFailed):
            token_crypto.decrypt("this-is-not-a-fernet-token")


class TestErrorsCarryNoSecrets:
    """An exception message is the most common way a credential reaches a log aggregator: it
    is caught, logged with `exc_info`, shipped, indexed, and retained for ninety days. So the
    messages are asserted to contain neither the token nor the key."""

    def test_the_not_configured_error_contains_no_token(self, monkeypatch):
        monkeypatch.setattr(settings, "token_encryption_key", None)
        with pytest.raises(token_crypto.TokenEncryptionNotConfigured) as caught:
            token_crypto.encrypt(_TOKEN)
        assert _TOKEN not in str(caught.value)

    def test_the_decryption_error_contains_neither_key_nor_ciphertext(self, monkeypatch):
        key = Fernet.generate_key().decode()
        monkeypatch.setattr(settings, "token_encryption_key", key)
        ciphertext = token_crypto.encrypt(_TOKEN)
        monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())

        with pytest.raises(token_crypto.TokenDecryptionFailed) as caught:
            token_crypto.decrypt(ciphertext)
        message = str(caught.value)
        assert key not in message
        assert ciphertext not in message
        assert _TOKEN not in message

    def test_the_malformed_key_error_does_not_echo_the_key(self, monkeypatch):
        bad_key = "definitely-not-a-fernet-key-but-secret-looking"
        monkeypatch.setattr(settings, "token_encryption_key", bad_key)
        with pytest.raises(token_crypto.TokenEncryptionNotConfigured) as caught:
            token_crypto.encrypt(_TOKEN)
        assert bad_key not in str(caught.value)
