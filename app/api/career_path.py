from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.career_path import (
    CareerPathProjectRead,
    CareerPathRead,
    CareerPathSummaryRead,
    PracticeHandoffRead,
    RoleCatalogEntryRead,
    SelectCareerRoleRequest,
    SkillItemRead,
    UpdateProjectRequest,
    UpdateSkillItemRequest,
)
from app.services.career_path import service as career_path_service
from app.services.career_path.role_catalog import CATEGORY_LIST, ROLE_FAMILIES
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/career-path", tags=["career-path"])


@router.get("/role-catalog", response_model=List[RoleCatalogEntryRead])
def get_role_catalog(category: Optional[str] = None, q: Optional[str] = None) -> List[RoleCatalogEntryRead]:
    """Browse/search the curated role catalog for the Role Selection screen. category/q are
    both optional client-side filters on top of the same fixed list -- omitting both returns
    every curated family. Custom/uncurated roles are never listed here (there's nothing to
    browse for them); the student types them directly into the "Enter custom role" field."""
    entries = [
        RoleCatalogEntryRead(
            family_key=family.family_key,
            display_name=family.display_name,
            category=family.category,
            short_explanation=family.short_explanation,
        )
        for family in ROLE_FAMILIES.values()
    ]
    if category:
        entries = [e for e in entries if e.category.lower() == category.lower()]
    if q:
        q_lower = q.lower()
        entries = [e for e in entries if q_lower in e.display_name.lower()]
    return entries


@router.get("/role-categories", response_model=List[str])
def get_role_categories() -> List[str]:
    return CATEGORY_LIST


@router.post("/generate", response_model=CareerPathRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
def generate(
    request: Request,
    payload: SelectCareerRoleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> CareerPathRead:
    """Selects (or re-selects) a target role and builds/rebuilds its Career Path content,
    making it the profile's current path. Works even with zero resume/profile data -- a
    role alone is always enough to produce a real, non-fabricated plan."""
    profile = get_or_create_profile(db, current_user)
    try:
        return career_path_service.select_role(
            db, profile.id, ai_provider, role_id=payload.role_id, custom_role=payload.custom_role
        )
    except career_path_service.RoleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/all", response_model=List[CareerPathSummaryRead])
def list_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[CareerPathSummaryRead]:
    """Every career path this profile has ever selected, most recently updated first -- the
    "recently selected roles" / Saved Career Paths screen's data source."""
    profile = get_or_create_profile(db, current_user)
    return career_path_service.list_all(db, profile.id)


@router.get("/current", response_model=Optional[CareerPathRead])
def get_current(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Optional[CareerPathRead]:
    profile = get_or_create_profile(db, current_user)
    return career_path_service.get_current(db, profile.id)


@router.get("/{path_id}", response_model=CareerPathRead)
def get_path(
    path_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerPathRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return career_path_service.get_path(db, profile.id, path_id)
    except career_path_service.CareerPathNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.put("/{path_id}/set-current", response_model=CareerPathRead)
@limiter.limit("20/hour")
def set_current(
    request: Request,
    path_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerPathRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return career_path_service.set_current(db, profile.id, path_id)
    except career_path_service.CareerPathNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{path_id}/refresh", response_model=CareerPathRead)
@limiter.limit("10/hour")
def refresh(
    request: Request,
    path_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> CareerPathRead:
    """Regenerates this path's content in place, carrying forward completion by stable key.
    Never changes which path is primary."""
    profile = get_or_create_profile(db, current_user)
    try:
        return career_path_service.refresh(db, profile.id, path_id, ai_provider)
    except career_path_service.CareerPathNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/{path_id}/skills", response_model=List[SkillItemRead])
def get_skills(
    path_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[SkillItemRead]:
    profile = get_or_create_profile(db, current_user)
    try:
        path_read = career_path_service.get_path(db, profile.id, path_id)
    except career_path_service.CareerPathNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return [item for phase in path_read.phases for item in phase.skill_items]


@router.get("/{path_id}/projects", response_model=List[CareerPathProjectRead])
def get_projects(
    path_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[CareerPathProjectRead]:
    profile = get_or_create_profile(db, current_user)
    try:
        path_read = career_path_service.get_path(db, profile.id, path_id)
    except career_path_service.CareerPathNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return [project for phase in path_read.phases for project in phase.projects]


@router.post("/skill-items/{item_id}/practice", response_model=PracticeHandoffRead)
@limiter.limit("20/hour")
def practice_skill(
    request: Request,
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PracticeHandoffRead:
    """Get-or-creates a single-skill AssessmentBlueprint scoped to this skill item, then
    hands off entirely to the existing, unmodified assessment flow
    (POST /assessments {blueprint_id})."""
    profile = get_or_create_profile(db, current_user)
    try:
        blueprint_id = career_path_service.get_or_create_practice_blueprint(db, profile.id, item_id)
        return PracticeHandoffRead(blueprint_id=blueprint_id)
    except career_path_service.SkillItemNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.patch("/skill-items/{item_id}", response_model=SkillItemRead)
def update_skill_item(
    item_id: int,
    payload: UpdateSkillItemRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SkillItemRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return career_path_service.mark_skill_item_status(db, profile.id, item_id, payload.status)
    except career_path_service.SkillItemNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.patch("/projects/{project_id}", response_model=CareerPathProjectRead)
def update_project(
    project_id: int,
    payload: UpdateProjectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerPathProjectRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return career_path_service.mark_project_status(db, profile.id, project_id, payload.status)
    except career_path_service.ProjectNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{path_id}/apply-jd/{jd_id}", response_model=CareerPathRead)
@limiter.limit("10/hour")
def apply_jd(
    request: Request,
    path_id: int,
    jd_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareerPathRead:
    """ "Update my Career Path using this JD" -- merges a specific job description's
    requirements into this path's skill items. Never touches JobPreparationPlan or any
    placement_path table; Career Path and JD Analyzer remain distinct concepts."""
    profile = get_or_create_profile(db, current_user)
    try:
        return career_path_service.apply_jd(db, profile.id, path_id, jd_id)
    except career_path_service.CareerPathNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except career_path_service.JobDescriptionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
