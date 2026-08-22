from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.assessment import AssessmentBlueprintRead, PrepareMeRequest
from app.services.assessment_service import InvalidPrepareMeRequestError, generate_blueprint
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/assessments", tags=["assessments"])


@router.post("/blueprint", response_model=AssessmentBlueprintRead, status_code=status.HTTP_201_CREATED)
def create_blueprint(
    payload: PrepareMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssessmentBlueprintRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return generate_blueprint(db, profile.id, payload.role_id, payload.jd_id)
    except InvalidPrepareMeRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
