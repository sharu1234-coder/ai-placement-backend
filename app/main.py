import contextlib
import logging
import threading
import time
from collections.abc import Sequence
from typing import Any

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

from app.ai.provider import AICapabilityError
from app.api.actions import router as actions_router
from app.api.arena import router as arena_router
from app.api.assessments import router as assessments_router
from app.api.auth import router as auth_router
from app.api.career_decisions import router as career_decisions_router
from app.api.career_goals import router as career_goals_router
from app.api.career_path import router as career_path_router
from app.api.career_strategy import router as career_strategy_router
from app.api.career_twin import router as career_twin_router
from app.api.coach import router as coach_router
from app.api.conversations import router as conversations_router
from app.api.deps import get_db
from app.api.interviews import router as interviews_router
from app.api.job_hunt import router as job_hunt_router
from app.api.jobs import router as jobs_router
from app.api.legal import router as legal_router
from app.api.missions import router as missions_router
from app.api.next_best_action import router as next_best_action_router
from app.api.notifications import router as notifications_router
from app.api.placement_alarm import router as placement_alarm_router
from app.api.placement_path import router as placement_path_router
from app.api.professional_presence import router as professional_presence_router
from app.api.progress import router as progress_router
from app.api.project_builder import router as project_builder_router
from app.api.recruiter_attack import router as recruiter_attack_router
from app.api.resumes import router as resumes_router
from app.api.roadmap import router as roadmap_router
from app.api.skill_passport import router as skill_passport_router
from app.api.subscriptions import router as subscriptions_router
from app.api.users import router as users_router
from app.api.voice_session import router as voice_session_router
from app.api.weaknesses import router as weaknesses_router
from app.core.app_check import verify_app_check_token
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

@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    """Warm the local speech model so the first student to speak does not pay for loading it.

    On a background daemon thread, and that is the whole design:

    - **Not awaited.** `WhisperModel(...)` is blocking CPU work. Awaiting it here would hold
      the event loop and delay the port opening by ~2s, trading a cost on one request for a
      cost on every restart.
    - **Daemon.** The process must still be able to exit while this is running; a non-daemon
      thread would hold shutdown open for the length of a model load.
    - **Failure is logged and ignored.** This is an optimisation, not a dependency. If the
      model cannot be loaded here it will fail the same way on the first transcription, where
      the error already has a request to be reported on and a typed error to carry it. Making
      startup fail would turn a slow first answer into a server that will not boot.

    `_get_whisper_model()` holds a lock and returns the cached instance, so a student who
    submits an answer while this is still running waits for the same load rather than starting
    a second one.
    """
    if settings.warm_speech_models_on_startup and settings.ai_provider == "ollama":

        def warm() -> None:
            try:
                from app.ai.ollama_provider import _get_whisper_model

                started = time.perf_counter()
                _get_whisper_model()
                logger.info(
                    "speech model warmed in %.2fs", time.perf_counter() - started
                )
            except Exception as exc:
                # Type only, never the exception text: a model path can contain a local
                # filesystem layout, and this line goes to the application log.
                logger.warning(
                    "speech model warm-up skipped (%s); the first spoken answer will load it",
                    type(exc).__name__,
                )

        threading.Thread(target=warm, name="warm-speech-model", daemon=True).start()
    yield


app = FastAPI(title=settings.app_name, lifespan=_lifespan)

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


# App Check is verified once here for every route on these routers (rather than repeated
# per-route) so a new endpoint added to an already-covered router is protected automatically.
# legal_router is deliberately excluded -- its two GET routes are public policy documents
# (no get_current_user either), meant to be readable from a plain browser link, not just the
# real Android app. See app/core/app_check.py: a no-op today (FIREBASE_APP_CHECK_ENABLED
# defaults to false) until a real Firebase service account is configured.
_app_check_dep = [Depends(verify_app_check_token)]
app.include_router(auth_router, dependencies=_app_check_dep)
app.include_router(users_router, dependencies=_app_check_dep)
app.include_router(resumes_router, dependencies=_app_check_dep)
app.include_router(jobs_router, dependencies=_app_check_dep)
app.include_router(job_hunt_router, dependencies=_app_check_dep)
app.include_router(assessments_router, dependencies=_app_check_dep)
app.include_router(interviews_router, dependencies=_app_check_dep)
app.include_router(conversations_router, dependencies=_app_check_dep)
app.include_router(roadmap_router, dependencies=_app_check_dep)
app.include_router(progress_router, dependencies=_app_check_dep)
app.include_router(legal_router)
app.include_router(placement_alarm_router, dependencies=_app_check_dep)
app.include_router(placement_path_router, dependencies=_app_check_dep)
app.include_router(career_path_router, dependencies=_app_check_dep)
app.include_router(career_twin_router, dependencies=_app_check_dep)
app.include_router(career_goals_router, dependencies=_app_check_dep)
app.include_router(career_strategy_router, dependencies=_app_check_dep)
app.include_router(career_decisions_router, dependencies=_app_check_dep)
app.include_router(skill_passport_router, dependencies=_app_check_dep)
app.include_router(next_best_action_router, dependencies=_app_check_dep)
app.include_router(missions_router, dependencies=_app_check_dep)
app.include_router(notifications_router, dependencies=_app_check_dep)
app.include_router(weaknesses_router, dependencies=_app_check_dep)
app.include_router(project_builder_router, dependencies=_app_check_dep)
app.include_router(recruiter_attack_router, dependencies=_app_check_dep)
app.include_router(arena_router, dependencies=_app_check_dep)
app.include_router(actions_router, dependencies=_app_check_dep)
app.include_router(coach_router, dependencies=_app_check_dep)
# NOT router-level here: subscriptions_router also owns POST /subscriptions/webhook, called
# by Razorpay's own servers (never the Android app), which will never carry an
# X-Firebase-AppCheck header -- see the per-route dependency on every other route in
# app/api/subscriptions.py instead.
app.include_router(subscriptions_router)
# Same exception, same reason: professional_presence_router owns GET
# /professional-presence/connections/{provider}/callback, which GitHub redirects the student's
# BROWSER to. That request carries no X-Firebase-AppCheck header (and no bearer token) because
# GitHub has neither, so router-level App Check would 401 every successful authorisation. Every
# other route on that router carries the dependency per-route -- see app/api/professional_presence.py.
app.include_router(professional_presence_router)
# Third exception, and a structural one rather than a caller quirk: voice_session_router owns a
# WebSocket route, and a router-level HTTP dependency that raises HTTPException has no defined
# behaviour on a WebSocket handshake. It calls verify_app_check_token itself, before accepting
# the socket, so attestation is enforced through the same function and simply fails the upgrade
# instead of raising into a protocol that cannot carry the error. See app/api/voice_session.py.
app.include_router(voice_session_router)


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
        logger.error("Readiness check failed: schema at %s, expected %s", current_revision, expected_revision)
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


def _validation_errors_without_input(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Pydantic's validation errors, minus the value the client actually submitted.

    Each error Pydantic produces carries an `input` key holding the offending value verbatim.
    Echoing it back is how `POST /auth/signup` with a too-short password answered 422 with that
    password in the response body -- and the same shape would return a pasted job description, a
    resume excerpt, or anything else a student typed into a field that failed validation.

    FastAPI materialises these errors into plain dicts before the handler sees them, so Pydantic's
    own `errors(include_input=False)` is not reachable here (`ValidationException.errors()` takes
    no arguments); the stripping has to happen on the dicts.

    What stays is everything a client needs to correct the request: `type`, `loc`, `msg` and the
    schema-side constraint metadata in `ctx` (`min_length`, `pattern`, ...). The one `ctx` key
    dropped is `error`, which holds the raised exception object rather than schema metadata --
    `jsonable_encoder` would render it to a string, and for a custom validator that string is
    whatever the validator chose to say about the input. Its text is already reproduced in `msg`,
    so nothing diagnostic is lost.
    """
    safe: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            # Not a shape this function can reason about, so it is dropped rather than passed
            # through: an unknown object here could carry the input in a field this never saw.
            continue
        cleaned = {key: value for key, value in error.items() if key != "input"}
        ctx = cleaned.get("ctx")
        if isinstance(ctx, dict):
            cleaned["ctx"] = {k: v for k, v in ctx.items() if k != "error"}
        safe.append(cleaned)
    return safe


@app.exception_handler(AICapabilityError)
async def ai_capability_exception_handler(request: Request, exc: AICapabilityError) -> JSONResponse:
    """A capability refusal is a server misconfiguration, not the student's fault.

    This handler exists because `requires_ai` raises during dependency resolution, which is
    outside the route body and therefore outside the `except AIGenerationError` that every AI
    route already has. Without it the refusal would reach the bare-Exception handler and be
    reported as a 500, which is both the wrong status and the wrong signal to an operator.

    503 rather than 422: nothing about the request is wrong, and the condition is fixed by
    configuring the server. The client message is fixed and provider-agnostic for the same
    reason as app/api/ai_errors.py -- the exception text names the provider and the missing
    capability, and that belongs in the log, not in a response any caller can trigger.
    """
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "AI provider cannot serve this route (provider=%s capability=%s reason=%s) "
        "on %s %s [request_id=%s]",
        exc.provider, exc.capability.value, exc.reason or "not declared",
        request.method, request.url.path, request_id,
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": "This feature is temporarily unavailable on the server.",
            "request_id": request_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "detail": "Validation failed",
                "errors": _validation_errors_without_input(exc.errors()),
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
