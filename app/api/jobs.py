from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.job import JobDescriptionParseResponse, JobMatchRead, ParseJDRequest, RequirementRead
from app.services.jd_service import (
    JobDescriptionInUseError,
    delete_job_description,
    get_requirements,
    parse_job_description,
)
from app.services.matching_service import match_resume_to_job
from app.services.profile_service import get_or_create_profile
from app.services.skill_matching import NoResumeAnalysisError

router = APIRouter(prefix="/jobs", tags=["jobs"])


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
