from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.weakness_record import WeaknessRecordRead
from app.services import weakness_record_service
from app.services.profile_service import get_or_create_profile

router = APIRouter(prefix="/weaknesses", tags=["weaknesses"])


@router.get("", response_model=List[WeaknessRecordRead])
def list_weaknesses(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[WeaknessRecordRead]:
    """Interview Memory's persisted view: interview-evaluation dimensions this student has
    ever been flagged weak on, each carrying real cross-interview history (occurrences,
    current status, most recent score) -- not just this interview's snapshot."""
    profile = get_or_create_profile(db, current_user)
    return weakness_record_service.list_for_profile(db, profile.id)
