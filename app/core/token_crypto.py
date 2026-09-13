"""Encryption at rest for provider credentials.

Built on `cryptography`'s Fernet, which is already a pinned dependency of this project (see
requirements.txt, where it sits in the transitive security-floor block) -- so this adds no new
package. Fernet is AES-128-CBC with an HMAC-SHA256 authentication tag, versioned, with the IV
and timestamp handled for us. It is deliberately not a hand-rolled construction: the failure
modes of getting AES wrong by hand are silent, and nothing in the test suite would notice.

What this module protects against is specific and worth stating, because encryption at rest is
often assumed to do more than it does. It protects a leaked database: a backup file, a dump
shared with a contractor, a restored snapshot on a developer's machine, a managed-database
console. In all of those the ciphertext is present and the key is not, because the key lives in
the process environment. It does NOT protect against an attacker who already has code
execution on the API host -- they can simply call decrypt(). That is not a gap in this design;
it is the boundary any at-rest scheme has.

Nothing here logs. Not the plaintext, not the ciphertext, not the key, not a fragment of any of
them, and not on the error paths either -- an exception message that quotes "the value that
failed to decrypt" is a credential in a log aggregator, which is the exact outcome the
specification forbids. The exceptions below carry no payload at all.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class TokenEncryptionError(Exception):
    """Base class. Deliberately carries no value from the failing operation."""


class TokenEncryptionNotConfigured(TokenEncryptionError):
    """TOKEN_ENCRYPTION_KEY is missing or is not a usable Fernet key.

    Raised rather than falling back to storing plaintext. A fallback would mean a
    misconfigured deployment silently writing provider access tokens to the database in the
    clear while every endpoint kept returning 200 -- the same class of quiet, data-losing
    default that the startup guards in app/core/config.py exist to refuse.
    """


class TokenDecryptionFailed(TokenEncryptionError):
    """The stored ciphertext could not be decrypted with the configured key.

    In practice this means the key was rotated or replaced without re-encrypting existing
    rows. The honest response is to treat the connection as needing re-authorisation, not to
    crash the request: the student can reconnect, and no data is lost that cannot be refetched.
    """


@lru_cache(maxsize=4)
def _cipher_for(key: str) -> Fernet:
    """Cached per key value rather than per process, so a test that swaps the configured key
    gets a matching cipher instead of the first one the process ever built."""
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        # str(exc) from Fernet does not contain the key, but it is not worth relying on that
        # continuing to be true across versions -- so nothing from the original is re-raised.
        raise TokenEncryptionNotConfigured(
            "TOKEN_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ) from exc


def is_configured() -> bool:
    """Whether credentials can be stored at all.

    Used by the production startup guard and by the feature's own availability check, so that
    an unconfigured deployment reports "GitHub connection is not available" rather than
    offering a button that fails once the student has already authorised on GitHub's site.
    """
    key = settings.token_encryption_key
    if not key:
        return False
    try:
        _cipher_for(key)
    except TokenEncryptionNotConfigured:
        return False
    return True


def encrypt(plaintext: str) -> str:
    key = settings.token_encryption_key
    if not key:
        raise TokenEncryptionNotConfigured(
            "TOKEN_ENCRYPTION_KEY is not set, so provider credentials cannot be stored. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return _cipher_for(key).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    key = settings.token_encryption_key
    if not key:
        raise TokenEncryptionNotConfigured(
            "TOKEN_ENCRYPTION_KEY is not set, so stored provider credentials cannot be read."
        )
    try:
        return _cipher_for(key).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise TokenDecryptionFailed(
            "A stored provider credential could not be decrypted with the current "
            "TOKEN_ENCRYPTION_KEY. If the key was rotated, affected connections must be "
            "re-authorised."
        ) from exc
