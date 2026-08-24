import logging

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.assessments import router as assessments_router
from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.interviews import router as interviews_router
from app.api.jobs import router as jobs_router
from app.api.legal import router as legal_router
from app.api.progress import router as progress_router
from app.api.resumes import router as resumes_router
from app.api.roadmap import router as roadmap_router
from app.api.users import router as users_router
from app.core.body_size_limit import MaxBodySizeMiddleware
from app.core.config import settings, validate_for_production
from app.core.logging import configure_logging
from app.core.observability import RequestObservabilityMiddleware
from app.core.rate_limit import limiter
from app.core.schema_check import get_expected_head_revision, is_schema_current
from app.core.security_headers import HSTSMiddleware

configure_logging()
logger = logging.getLogger(__name__)

validate_for_production()

app = FastAPI(title=settings.app_name)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# 12MB: just above the 10MB per-file caps already enforced by the streaming upload helper
# (app/api/upload_utils.py) for resumes/interview audio, so it never interferes with those;
# every other route (JSON bodies) previously had no cap at all. See
# app/core/body_size_limit.py for why this checks both Content-Length and the actual
# streamed byte count.
app.add_middleware(MaxBodySizeMiddleware, max_body_size=12 * 1024 * 1024)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[host.strip() for host in settings.allowed_hosts.split(",")],
)

app.add_middleware(HSTSMiddleware)

# Outermost middleware (added last -- see RequestObservabilityMiddleware's docstring for why
# order matters here): observes and times the final response to every request, including
# ones the middlewares above reject before a route is ever reached.
app.add_middleware(RequestObservabilityMiddleware)


app.include_router(auth_router)
app.include_router(users_router)
app.include_router(resumes_router)
app.include_router(jobs_router)
app.include_router(assessments_router)
app.include_router(interviews_router)
app.include_router(roadmap_router)
app.include_router(progress_router)
app.include_router(legal_router)


@app.get("/health")
def health_check() -> dict:
    """Liveness only -- is the process up. No dependency checks, so this never fails just
    because the database is briefly unreachable (see /health/ready for that)."""
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


@app.get("/health/ready")
def readiness_check(db: Session = Depends(get_db)) -> JSONResponse:
    """Readiness -- can this instance actually serve traffic right now. Checks the database
    the same way a real request would (via get_db, so it reflects the configured
    DATABASE_URL/session exactly, and is testable via the same dependency override every
    other test already uses), and that the database's schema is actually at the migration
    revision this deployment's code expects -- catches "deployed new code but forgot to run
    `alembic upgrade head`" (the process starts fine and /health passes; only this notices)."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # Logs only the exception type, not str(exc) -- some drivers' connection-failure
        # messages can echo back the DSN (and its embedded password) they failed to connect
        # with.
        logger.error("Readiness check failed: database unreachable (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})

    try:
        current_revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        db.rollback()  # failed statement leaves the session unusable until rolled back
        current_revision = None

    expected_revision = get_expected_head_revision()
    if not is_schema_current(current_revision, expected_revision):
        logger.error(
            "Readiness check failed: schema at %s, expected %s", current_revision, expected_revision
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "database": "reachable",
                "schema": "out_of_date",
                "current_revision": current_revision,
                "expected_revision": expected_revision,
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "database": "reachable",
            "schema": "current" if current_revision else "not_tracked",
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "detail": "Validation failed",
                "errors": exc.errors(),
                "request_id": getattr(request.state, "request_id", None),
            }
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # request_id lets a user/support report this exact ID and have it found in server logs
    # via RequestObservabilityMiddleware's log line for the same request.
    #
    # Starlette special-cases a bare-Exception handler: it runs inside ServerErrorMiddleware,
    # which sits outside every app.add_middleware() layer (including
    # RequestObservabilityMiddleware) -- the one path where that middleware's own response
    # header injection never gets a chance to run. So the X-Request-ID header is set directly
    # here too, to keep this response consistent with every other response.
    request_id = getattr(request.state, "request_id", None)
    logger.exception("Unhandled exception on %s %s [request_id=%s]", request.method, request.url.path, request_id)
    headers = {"X-Request-ID": request_id} if request_id else {}
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
        headers=headers,
    )
