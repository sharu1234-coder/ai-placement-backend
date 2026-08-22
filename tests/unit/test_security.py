import jwt
import pytest

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_and_verify_password_roundtrip() -> None:
    password_hash = hash_password("supersecret123")
    assert password_hash != "supersecret123"
    assert verify_password("supersecret123", password_hash)
    assert not verify_password("wrong-password", password_hash)


def test_access_token_roundtrip() -> None:
    token = create_access_token(subject="42")
    payload = decode_token(token)
    assert payload["sub"] == "42"
    assert payload["type"] == "access"


def test_refresh_token_roundtrip() -> None:
    token = create_refresh_token(subject="7")
    payload = decode_token(token)
    assert payload["sub"] == "7"
    assert payload["type"] == "refresh"


def test_decode_invalid_token_raises() -> None:
    with pytest.raises(jwt.PyJWTError):
        decode_token("not-a-real-token")
