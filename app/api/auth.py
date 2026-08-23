import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.email.provider import EmailProvider, get_email_provider
from app.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
)
from app.schemas.user import UserRead
from app.services.password_reset_service import request_password_reset, reset_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=UserRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("3/hour")
def signup(request: Request, payload: SignupRequest, db: Session = Depends(get_db)) -> User:
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def login(request: Request, payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    return TokenResponse(
        access_token=create_access_token(subject=str(user.id), token_version=user.token_version),
        refresh_token=create_refresh_token(subject=str(user.id), token_version=user.token_version),
    )


@router.post("/logout", response_model=MessageResponse)
def logout(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> MessageResponse:
    """Bumps token_version, which immediately invalidates every access/refresh token already
    issued to this user (checked in app/api/deps.get_current_user and /auth/refresh) --
    logs out every session/device at once, not just the one that called this endpoint.
    There's no per-device/session tracking anywhere else in this app to do anything finer-
    grained than that yet."""
    current_user.token_version += 1
    db.commit()
    return MessageResponse(message="Logged out.")


@router.post("/forgot-password", response_model=MessageResponse)
@limiter.limit("3/hour")
def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
    email_provider: EmailProvider = Depends(get_email_provider),
) -> MessageResponse:
    """Always returns the same message whether or not the email is registered -- see
    password_reset_service.request_password_reset for why (prevents email enumeration)."""
    request_password_reset(db, payload.email, email_provider)
    return MessageResponse(message="If that email is registered, a reset code has been sent.")


@router.post("/reset-password", response_model=MessageResponse)
@limiter.limit("10/minute")
def reset_password_endpoint(
    request: Request, payload: ResetPasswordRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    success = reset_password(db, payload.email, payload.code, payload.new_password)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset code"
        )
    return MessageResponse(message="Password has been reset.")


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("20/minute")
def refresh(request: Request, payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    invalid_token_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
    )
    try:
        decoded = decode_token(payload.refresh_token)
    except jwt.PyJWTError:
        raise invalid_token_error

    if decoded.get("type") != "refresh":
        raise invalid_token_error

    try:
        user_id = int(decoded["sub"])
    except (KeyError, ValueError):
        raise invalid_token_error

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise invalid_token_error

    if decoded.get("tv") != user.token_version:
        # Refresh token was issued before a logout/password-reset -- same check as
        # get_current_user, applied here too since /auth/refresh doesn't go through it.
        raise invalid_token_error

    return TokenResponse(
        access_token=create_access_token(subject=str(user.id), token_version=user.token_version),
        refresh_token=create_refresh_token(subject=str(user.id), token_version=user.token_version),
    )
