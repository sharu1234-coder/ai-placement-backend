import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.project_builder import (
    CheckGithubRequest,
    CompleteStageRequest,
    CreateProjectBuildRequest,
    GitHubEvidenceRead,
    ProjectBuildListItemRead,
    ProjectBuildRead,
    ProjectBuildStageRead,
    ProjectProofRead,
)
from app.services import github_verification_service, idempotency
from app.services.profile_service import get_or_create_profile
from app.services.project_builder import service as project_builder_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/project-builder", tags=["project-builder"])


@router.post("", response_model=ProjectBuildRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
def create_build(
    request: Request,
    payload: CreateProjectBuildRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> ProjectBuildRead:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[ProjectBuildRead, int]:
        created = project_builder_service.create_build(
            db, profile, payload.title, payload.stack_summary, ai_provider
        )
        return created, created.id

    try:
        # The most expensive create in the app: one build plus ten stages, each with generated
        # guidance, so a replay is a duplicate project *and* a second full generation pass. The
        # claim commits before perform() runs, so Phase 19's no-transaction-across-inference
        # rule still holds when create_build opens its own boundary.
        return idempotency.run_once(
            db,
            profile.id,
            operation="project_build.create",
            idempotency_key=idempotency_key,
            params={"title": payload.title, "stack_summary": payload.stack_summary},
            perform=perform,
            read_back=lambda rid: project_builder_service.read_build(db, profile.id, rid),
            result_kind="project_build",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "project_build.create")


@router.get("", response_model=List[ProjectBuildListItemRead])
def list_builds(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[ProjectBuildListItemRead]:
    profile = get_or_create_profile(db, current_user)
    return project_builder_service.list_builds(db, profile.id)


@router.get("/{build_id}", response_model=ProjectBuildRead)
def get_build(
    build_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectBuildRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return project_builder_service.get_build(db, profile.id, build_id)
    except project_builder_service.ProjectBuildNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{build_id}/stages/{stage_key}/start", response_model=ProjectBuildStageRead)
def start_stage(
    build_id: int,
    stage_key: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectBuildStageRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return project_builder_service.start_stage(db, profile, build_id, stage_key)
    except project_builder_service.ProjectBuildNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except project_builder_service.StageNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except project_builder_service.StageLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{build_id}/stages/{stage_key}/complete", response_model=ProjectBuildStageRead)
@limiter.limit("60/hour")
def complete_stage(
    request: Request,
    build_id: int,
    stage_key: str,
    payload: CompleteStageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> ProjectBuildStageRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return project_builder_service.complete_stage(db, profile, build_id, stage_key, payload.notes, ai_provider)
    except project_builder_service.ProjectBuildNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except project_builder_service.StageNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except project_builder_service.StageLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/{build_id}/proof", response_model=ProjectProofRead)
def get_proof(
    build_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectProofRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return project_builder_service.get_proof(db, profile.id, build_id)
    except project_builder_service.ProjectBuildNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except project_builder_service.ProofNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{build_id}/github-check", response_model=GitHubEvidenceRead)
@limiter.limit("10/hour")
def check_github(
    request: Request,
    build_id: int,
    payload: CheckGithubRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitHubEvidenceRead:
    """Unauthenticated, public-repos-only GitHub check -- rate-limited on top of GitHub's own
    60-requests/hour/IP unauthenticated limit, which is shared across every student using this
    server, not per-user."""
    profile = get_or_create_profile(db, current_user)
    try:
        return project_builder_service.check_github(db, profile, build_id, payload.repo_url)
    except project_builder_service.ProjectBuildNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except github_verification_service.InvalidRepoUrlError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except github_verification_service.GitHubApiError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
