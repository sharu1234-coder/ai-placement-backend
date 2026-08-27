from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.job import (
    GeneratePreparationPlanRequest,
    JobDescriptionListItem,
    JobDescriptionParseResponse,
    JobMatchRead,
    JobPreparationPlanRead,
    ParseJDRequest,
    RequirementRead,
)
from app.services.jd_service import (
    JobDescriptionInUseError,
    delete_job_description,
    generate_preparation_plan,
    get_preparation_plan,
    get_requirements,
    list_job_descriptions,
    parse_job_description,
)
from app.services.matching_service import match_resume_to_job
from app.services.profile_service import get_or_create_profile
from app.services.skill_matching import NoResumeAnalysisError

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobDescriptionListItem])
def list_jobs(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[JobDescriptionListItem]:
    profile = get_or_create_profile(db, current_user)
    return list_job_descriptions(db, profile.id)


@router.post("", response_model=JobDescriptionParseResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
def create_job_description(
    request: Request,
    payload: ParseJDRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> JobDescriptionParseResponse:
    profile = get_or_create_profile(db, current_user)
    return parse_job_description(db, profile.id, payload.raw_text, ai_provider)


@router.get("/{jd_id}/requirements", response_model=list[RequirementRead])
def get_job_requirements(
    jd_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RequirementRead]:
    profile = get_or_create_profile(db, current_user)
    requirements = get_requirements(db, jd_id, profile.id)
    if requirements is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job description not found")
    return requirements


@router.post("/{jd_id}/match", response_model=JobMatchRead)
def match_job(
    jd_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobMatchRead:
    profile = get_or_create_profile(db, current_user)
    try:
        result = match_resume_to_job(db, jd_id, profile.id)
    except NoResumeAnalysisError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job description not found")
    return result


@router.post("/{jd_id}/preparation-plan", response_model=JobPreparationPlanRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/hour")
def create_preparation_plan(
    request: Request,
    jd_id: int,
    payload: GeneratePreparationPlanRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> JobPreparationPlanRead:
    profile = get_or_create_profile(db, current_user)
    try:
        result = generate_preparation_plan(
            db, profile.id, jd_id, ai_provider, payload.company_name, payload.job_title
        )
    except NoResumeAnalysisError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job description not found")
    return result


@router.get("/{jd_id}/preparation-plan", response_model=JobPreparationPlanRead)
def read_preparation_plan(
    jd_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobPreparationPlanRead:
    profile = get_or_create_profile(db, current_user)
    result = get_preparation_plan(db, jd_id, profile.id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No preparation plan found")
    return result


@router.delete("/{jd_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job_description_endpoint(
    jd_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    profile = get_or_create_profile(db, current_user)
    try:
        deleted = delete_job_description(db, jd_id, profile.id)
    except JobDescriptionInUseError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if deleted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job description not found")
