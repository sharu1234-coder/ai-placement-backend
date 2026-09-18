from collections.abc import Generator

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.rate_limit import RATE_LIMIT_SUBJECT_ATTR
from app.core.security import decode_token
from app.database.session import SessionLocal
from app.models.user import User

bearer_scheme = HTTPBearer()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
    )
    try:
        payload = decode_token(credentials.credentials)
    except jwt.PyJWTError:
        raise credentials_error

    if payload.get("type") != "access":
        raise credentials_error

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise credentials_error

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise credentials_error
    if payload.get("tv") != user.token_version:
        # Token was valid and unexpired but was issued before the user's most recent
        # logout/password-reset (app/api/auth.py's logout endpoint,
        # app/services/password_reset_service.py's reset_password both bump token_version).
        raise credentials_error

    # Rate-limit identity, published only now -- past every check above, so it can never be set
    # for a request whose token was forged, expired, revoked, of the wrong type, or belongs to
    # a deactivated user. app/core/rate_limit.user_or_ip_key reads it and falls back to the
    # client address when it is absent, so failing to reach this line means the request is
    # limited by IP rather than escaping limits altogether.
    setattr(request.state, RATE_LIMIT_SUBJECT_ATTR, user.id)
    return user


def require_premium(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Server-side enforcement for the "advanced tier" features the Android app already gates
    behind PaywallGate (Career Decision Lab in full; Career Twin's evidence-breakdown reasons).
    PaywallGate only ever hides UI -- without this, any authenticated free user could call the
    underlying endpoint directly and get the full premium response. Local imports avoid a
    module-load-time circular import between the API layer and the service layer."""
    from app.services.profile_service import get_or_create_profile
    from app.services.subscription_service import is_premium

    profile = get_or_create_profile(db, current_user)
    if not is_premium(db, profile.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This feature requires an active Premium subscription",
        )
    return current_user
