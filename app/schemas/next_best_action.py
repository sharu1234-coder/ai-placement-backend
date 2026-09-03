from typing import List, Optional

from pydantic import BaseModel


class NextBestActionRead(BaseModel):
    has_recommendation: bool
    # critical_role_gap | repeated_weakness | assessment_weakness | interview_weakness |
    # project_requirement | resume_jd_gap | roadmap_dependency | stale_skill | maintenance
    tier: Optional[str] = None
    title: str
    action_type: str  # learn | practice | project | resume | roadmap | maintenance
    # A fixed estimate per action_type, not a per-recommendation measurement -- there's no
    # real time-tracking data anywhere in this codebase to base a precise number on.
    estimated_minutes: Optional[int] = None
    reasons: List[str] = []
    skill: Optional[str] = None
