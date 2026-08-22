import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.assessments import router as assessments_router
from app.api.auth import router as auth_router
from app.api.interviews import router as interviews_router
from app.api.jobs import router as jobs_router
from app.api.resumes import router as resumes_router
from app.api.roadmap import router as roadmap_router
from app.api.users import router as users_router
from app.core.config import settings
from app.core.logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(resumes_router)
app.include_router(jobs_router)
app.include_router(assessments_router)
app.include_router(interviews_router)
app.include_router(roadmap_router)


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


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
