import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.idempotency_errors import idempotency_http_error
from app.core.rate_limit import limiter
from app.core.security import verify_password
from app.models.certification import Certification
from app.models.education import Education
from app.models.experience import Experience
from app.models.project import Project
from app.models.role import Role
from app.models.skill import Skill
from app.models.user import User
from app.schemas.profile import (
    RoleRead,
    RoleSelectRequest,
    SecondaryRoleSelectRequest,
    StudentProfileRead,
    StudentProfileUpsert,
)
from app.schemas.profile_items import (
    CertificationCreate,
    CertificationRead,
    EducationCreate,
    EducationRead,
    ExperienceCreate,
    ExperienceRead,
    ProjectCreate,
    ProjectRead,
    SkillCreate,
    SkillRead,
)
from app.schemas.user import AccountDeleteRequest
from app.services import idempotency
from app.services.account_service import delete_account
from app.services.profile_service import get_or_create_profile, profile_read
from app.storage.provider import StorageProvider, get_storage_provider

logger = logging.getLogger(__name__)

router = APIRouter(tags=["users"])


def _delete_owned(db: Session, model, item_id: int, profile_id: int) -> None:
    """Shared by the education/skills/projects/experience/certifications DELETE routes
    below -- PROJECT_HANDOFF.md section 6 requires "deletion controls for user data";
    these sub-resources previously had no delete path at all."""
    item = db.get(model, item_id)
    if item is None or item.profile_id != profile_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.delete(item)
    db.commit()


@router.get("/roles", response_model=list[RoleRead])
def list_roles(db: Session = Depends(get_db)) -> list[Role]:
    return db.query(Role).order_by(Role.name).all()


@router.get("/users/me", response_model=StudentProfileRead)
def get_my_profile(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> StudentProfileRead:
    return profile_read(get_or_create_profile(db, current_user), current_user)


@router.put("/users/me", response_model=StudentProfileRead)
def update_my_profile(
    payload: StudentProfileUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudentProfileRead:
    profile = get_or_create_profile(db, current_user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    db.commit()
    db.refresh(profile)
    return profile_read(profile, current_user)


@router.delete("/users/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_account(
    payload: AccountDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageProvider = Depends(get_storage_provider),
) -> None:
    """Complete account deletion (PROJECT_HANDOFF.md section 6 / production-readiness item
    5) -- requires the current password so a stolen/leaked access token alone can't trigger
    an irreversible wipe. Erases the profile and every row derived from it (resumes,
    assessments, interviews, roadmaps, job matches/descriptions), not just the user row --
    see app/services/account_service.py for why each table is safe to remove this way."""
    if not verify_password(payload.password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Incorrect password")
    delete_account(db, current_user, storage)


@router.post("/users/me/role", response_model=StudentProfileRead)
def select_role(
    payload: RoleSelectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudentProfileRead:
    """Primary target role -- either a catalog pick (role_id) or a free-text role the
    student typed in (custom_role), mutually exclusive (RoleSelectRequest enforces
    exactly one is set). Picking one clears the other."""
    profile = get_or_create_profile(db, current_user)

    if payload.role_id is not None:
        role = db.get(Role, payload.role_id)
        if role is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
        profile.target_role_id = role.id
        profile.custom_target_role = None
    else:
        profile.target_role_id = None
        profile.custom_target_role = payload.custom_role

    db.commit()
    db.refresh(profile)
    return profile_read(profile, current_user)


@router.post("/users/me/role/secondary", response_model=StudentProfileRead)
def select_secondary_role(
    payload: SecondaryRoleSelectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudentProfileRead:
    role = db.get(Role, payload.role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    profile = get_or_create_profile(db, current_user)
    profile.secondary_target_role_id = role.id
    db.commit()
    db.refresh(profile)
    return profile_read(profile, current_user)


@router.post("/users/me/onboarding/complete", response_model=StudentProfileRead)
def complete_onboarding(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudentProfileRead:
    """Idempotent -- marks onboarding done on first call, a no-op (same timestamp) on
    any repeat call. A dedicated endpoint rather than a field on the general PUT
    /users/me upsert so there's no ambiguity about ever "uncompleting" onboarding via
    an omitted field."""
    profile = get_or_create_profile(db, current_user)
    if profile.onboarding_completed_at is None:
        profile.onboarding_completed_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
        db.refresh(profile)
    return profile_read(profile, current_user)


@router.post(
    "/users/me/education", response_model=EducationRead, status_code=status.HTTP_201_CREATED
)
def add_education(
    payload: EducationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Education:
    profile = get_or_create_profile(db, current_user)
    item = Education(profile_id=profile.id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/users/me/education", response_model=list[EducationRead])
def list_education(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[Education]:
    profile = get_or_create_profile(db, current_user)
    return db.query(Education).filter(Education.profile_id == profile.id).all()


@router.delete("/users/me/education/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_education(
    item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    profile = get_or_create_profile(db, current_user)
    _delete_owned(db, Education, item_id, profile.id)


@router.post("/users/me/skills", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def add_skill(
    payload: SkillCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Skill:
    profile = get_or_create_profile(db, current_user)

    def perform() -> tuple[Skill, int]:
        item = Skill(profile_id=profile.id, **payload.model_dump())
        db.add(item)
        db.commit()
        db.refresh(item)
        return item, item.id

    def read_back(skill_id: int) -> Optional[Skill]:
        existing = db.get(Skill, skill_id)
        # Ownership re-checked rather than inferred from the claim, so a result id can never be
        # read across profiles even if a key were somehow shared.
        return existing if existing is not None and existing.profile_id == profile.id else None

    try:
        # Nothing deduplicates skills, so a replay leaves the same skill listed twice on the
        # student's profile -- visible, and theirs to clean up.
        return idempotency.run_once(
            db,
            profile.id,
            operation="profile.add_skill",
            idempotency_key=idempotency_key,
            params=payload.model_dump(),
            perform=perform,
            read_back=read_back,
            result_kind="skill",
        )
    except idempotency.IdempotencyError as exc:
        raise idempotency_http_error(exc, logger, "profile.add_skill")


@router.get("/users/me/skills", response_model=list[SkillRead])
def list_skills(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[Skill]:
    profile = get_or_create_profile(db, current_user)
    return db.query(Skill).filter(Skill.profile_id == profile.id).all()


@router.delete("/users/me/skills/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    profile = get_or_create_profile(db, current_user)
    _delete_owned(db, Skill, item_id, profile.id)


@router.post(
    "/users/me/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED
)
@limiter.limit("20/hour")
def add_project(
    request: Request,
    payload: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Project:
    profile = get_or_create_profile(db, current_user)
    item = Project(profile_id=profile.id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/users/me/projects", response_model=list[ProjectRead])
def list_projects(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[Project]:
    profile = get_or_create_profile(db, current_user)
    return db.query(Project).filter(Project.profile_id == profile.id).all()


@router.delete("/users/me/projects/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    profile = get_or_create_profile(db, current_user)
    _delete_owned(db, Project, item_id, profile.id)


@router.post(
    "/users/me/experience", response_model=ExperienceRead, status_code=status.HTTP_201_CREATED
)
def add_experience(
    payload: ExperienceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Experience:
    profile = get_or_create_profile(db, current_user)
    item = Experience(profile_id=profile.id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/users/me/experience", response_model=list[ExperienceRead])
def list_experience(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[Experience]:
    profile = get_or_create_profile(db, current_user)
    return db.query(Experience).filter(Experience.profile_id == profile.id).all()


@router.delete("/users/me/experience/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_experience(
    item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    profile = get_or_create_profile(db, current_user)
    _delete_owned(db, Experience, item_id, profile.id)


@router.post(
    "/users/me/certifications",
    response_model=CertificationRead,
    status_code=status.HTTP_201_CREATED,
)
def add_certification(
    payload: CertificationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Certification:
    profile = get_or_create_profile(db, current_user)
    item = Certification(profile_id=profile.id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/users/me/certifications", response_model=list[CertificationRead])
def list_certifications(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[Certification]:
    profile = get_or_create_profile(db, current_user)
    return db.query(Certification).filter(Certification.profile_id == profile.id).all()


@router.delete("/users/me/certifications/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_certification(
    item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    profile = get_or_create_profile(db, current_user)
    _delete_owned(db, Certification, item_id, profile.id)
