"""Firebase App Check verification -- confirms a request actually came from this app's real,
unmodified Android build (via Play Integrity in release, or the Debug provider in dev), not a
scripted client hitting the API directly with a stolen or forged bearer token. This is
defense-in-depth *in addition to* JWT auth (app/api/deps.py), not a replacement for it: App
Check verifies the calling app, JWT auth verifies the calling user.

Off by default (see Settings.firebase_app_check_enabled in app/core/config.py) so turning this
module on can never break an existing deployment that hasn't provisioned a Firebase service
account yet. Enabling it in production requires:
  1. A Firebase service account JSON (Firebase Console > Project Settings > Service Accounts),
     with its path set via the standard GOOGLE_APPLICATION_CREDENTIALS environment variable
     (picked up automatically by Application Default Credentials -- no app-specific setting
     for it, to avoid a second, driftable place this could be configured).
  2. FIREBASE_APP_CHECK_ENABLED=true.
  3. The Android app already sends the token (see NetworkModule.kt's AppCheckInterceptor) --
     true as of this same change, but confirm the deployed app version actually includes it
     before enabling enforcement, or every request from an older installed build will start
     failing with 401.

firebase_admin is imported lazily (inside functions, never at module load) specifically so
that importing this module -- which happens unconditionally via app/main.py -- never requires
a working firebase_admin/cryptography native install in an environment that never turns this
feature on. `_get_app_check_module` exists as its own function (rather than inlining the
import) purely so tests can monkeypatch it and exercise the verify/reject logic without ever
touching the real Firebase Admin SDK or needing real credentials.
"""

import logging
from typing import Optional

from fastapi import Header, HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)

_initialized = False


def _get_app_check_module():
    from firebase_admin import app_check

    return app_check


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    import firebase_admin

    if not firebase_admin._apps:  # no default app registered yet
        firebase_admin.initialize_app()
    _initialized = True


async def verify_app_check_token(
    x_firebase_appcheck: Optional[str] = Header(default=None),
) -> None:
    """FastAPI dependency -- attach via `dependencies=[Depends(verify_app_check_token)]` on an
    include_router() call (see app/main.py) rather than per-route, so new routes are covered
    automatically. A no-op whenever the feature is off, which is the default."""
    if not settings.firebase_app_check_enabled:
        return

    if not x_firebase_appcheck:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing App Check token.")

    try:
        _ensure_initialized()
        app_check = _get_app_check_module()
        app_check.verify_token(x_firebase_appcheck)
    except HTTPException:
        raise
    except Exception as exc:
        # Deliberately broad: PyJWKClientError, ValueError, and every jwt.* exception
        # app_check re-exports (ExpiredSignatureError, InvalidSignatureError, ...) all mean
        # the same thing here -- "this token doesn't prove a genuine app instance" -- and all
        # of them must fail closed (401), never surface as an unhandled 500.
        logger.warning("Rejected a request with an invalid App Check token: %s", type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid App Check token.") from exc
