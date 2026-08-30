import asyncio

import pytest
from fastapi import HTTPException

from app.core import app_check
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Every test starts from a clean slate: feature off, lazy-init flag cleared, and the
    Firebase Admin module getter never actually imported (see app_check.py's docstring for
    why this must stay mockable without a real Firebase Admin install)."""
    monkeypatch.setattr(settings, "firebase_app_check_enabled", False)
    monkeypatch.setattr(app_check, "_initialized", False)


def _run(coro):
    # verify_app_check_token is `async def` (required for it to be usable as a FastAPI
    # dependency) but its body never actually awaits anything -- asyncio.run() here avoids
    # pulling in pytest-asyncio (not a dependency of this project) just for one module.
    return asyncio.run(coro)


def test_disabled_by_default_never_touches_firebase_admin(monkeypatch):
    """The default (and every existing deployment until explicitly configured) must never
    even attempt to import firebase_admin -- asserted here by making that import explode if
    it's ever reached."""

    def _boom():
        raise AssertionError("firebase_admin should never be imported while the feature is off")

    monkeypatch.setattr(app_check, "_get_app_check_module", _boom)
    _run(app_check.verify_app_check_token(x_firebase_appcheck=None))  # no raise = pass


def test_enabled_missing_header_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "firebase_app_check_enabled", True)

    with pytest.raises(HTTPException) as exc_info:
        _run(app_check.verify_app_check_token(x_firebase_appcheck=None))
    assert exc_info.value.status_code == 401


def test_enabled_valid_token_passes_through(monkeypatch):
    monkeypatch.setattr(settings, "firebase_app_check_enabled", True)
    monkeypatch.setattr(app_check, "_ensure_initialized", lambda: None)

    class _FakeAppCheckModule:
        def verify_token(self, token):
            assert token == "genuine-token"
            return {"sub": "some-app-id"}

    monkeypatch.setattr(app_check, "_get_app_check_module", lambda: _FakeAppCheckModule())

    _run(app_check.verify_app_check_token(x_firebase_appcheck="genuine-token"))  # no raise = pass


def test_enabled_invalid_token_is_rejected_not_500(monkeypatch):
    """verify_token can raise several different exception types (ValueError, PyJWKClientError,
    various jwt.* errors) -- all of them must fail closed as 401, never bubble up as an
    unhandled 500 that would (a) leak a stack trace and (b) never actually reject the caller
    once FastAPI's default exception handling took over."""
    monkeypatch.setattr(settings, "firebase_app_check_enabled", True)
    monkeypatch.setattr(app_check, "_ensure_initialized", lambda: None)

    class _FakeAppCheckModule:
        def verify_token(self, token):
            raise ValueError("token signature invalid")

    monkeypatch.setattr(app_check, "_get_app_check_module", lambda: _FakeAppCheckModule())

    with pytest.raises(HTTPException) as exc_info:
        _run(app_check.verify_app_check_token(x_firebase_appcheck="forged-token"))
    assert exc_info.value.status_code == 401


def test_ensure_initialized_only_calls_initialize_app_once(monkeypatch):
    calls = []

    class _FakeFirebaseAdmin:
        _apps = {}

        @staticmethod
        def initialize_app():
            calls.append(1)
            _FakeFirebaseAdmin._apps = {"[DEFAULT]": object()}

    monkeypatch.setitem(__import__("sys").modules, "firebase_admin", _FakeFirebaseAdmin)

    app_check._ensure_initialized()
    app_check._ensure_initialized()

    assert calls == [1]
