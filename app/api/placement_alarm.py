from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.placement_alarm import (
    AlarmEligibilityRead,
    ChallengeAnswerResult,
    ChallengeStateRead,
    StartChallengeRequest,
    SubmitChallengeAnswerRequest,
)
from app.services import placement_alarm_service
from app.services.profile_service import get_or_create_profile
from app.services.skill_matching import NoResumeAnalysisError

router = APIRouter(prefix="/placement-alarm", tags=["placement-alarm"])


@router.get("/eligibility", response_model=AlarmEligibilityRead)
def check_eligibility(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AlarmEligibilityRead:
    profile = get_or_create_profile(db, current_user)
    return AlarmEligibilityRead(eligible=placement_alarm_service.has_eligible_skills(db, profile.id))


@router.post("/challenges", response_model=ChallengeStateRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/hour")
def start_challenge(
    request: Request,
    payload: StartChallengeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> ChallengeStateRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return placement_alarm_service.start_challenge(
            db, profile.id, payload.client_alarm_id, ai_provider
        )
    except NoResumeAnalysisError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except placement_alarm_service.ChallengeGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/challenges/{challenge_id}", response_model=ChallengeStateRead)
def get_challenge(
    challenge_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChallengeStateRead:
    profile = get_or_create_profile(db, current_user)
    try:
        return placement_alarm_service.get_challenge_state(db, profile.id, challenge_id)
    except placement_alarm_service.ChallengeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/challenges/{challenge_id}/answer", response_model=ChallengeAnswerResult)
@limiter.limit("60/hour")
def submit_answer(
    request: Request,
    challenge_id: int,
    payload: SubmitChallengeAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChallengeAnswerResult:
    profile = get_or_create_profile(db, current_user)
    try:
        return placement_alarm_service.submit_challenge_answer(
            db, profile.id, challenge_id, payload.question_id, payload.student_answer
        )
    except placement_alarm_service.ChallengeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except placement_alarm_service.QuestionNotInChallengeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except placement_alarm_service.QuestionAlreadyCorrectError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
