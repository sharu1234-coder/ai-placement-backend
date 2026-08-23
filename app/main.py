import logging

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
from app.core.config import settings
from app.core.logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[host.strip() for host in settings.allowed_hosts.split(",")],
)


@app.middleware("http")
async def add_hsts_header(request: Request, call_next):
    """Adds Strict-Transport-Security only for requests that actually arrived over HTTPS in
    production -- reads request.url.scheme fresh per request (not baked in at import), so
    it reflects reality even behind a reverse proxy (which requires Uvicorn's
    --proxy-headers flag to correctly report "https" here -- see README). Harmless no-op in
    dev, where every request is plain HTTP."""
    response = await call_next(request)
    if settings.environment == "production" and request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


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
    other test already uses)."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.error("Readiness check failed: database unreachable: %s", exc)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})
    return JSONResponse(status_code=200, content={"status": "ok", "database": "reachable"})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": "Validation failed", "errors": exc.errors()}),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
