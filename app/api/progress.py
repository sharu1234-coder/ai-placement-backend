from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.progress import ProgressDashboardRead
from app.services import progress_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/progress", tags=["progress"])


@router.get("/dashboard", response_model=ProgressDashboardRead)
def get_dashboard(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProgressDashboardRead:
    profile = get_or_create_profile(db, current_user)
    return progress_service.get_dashboard(db, profile.id)
