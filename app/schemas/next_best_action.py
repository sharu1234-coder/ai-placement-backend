from typing import List, Optional

from pydantic import BaseModel

# The one section this endpoint gates. Named rather than merely arriving empty, so a free
# client can show an upgrade prompt instead of an empty state -- the same convention, and the
# same reason, as career_strategy's LOCKABLE_SECTIONS.
LOCKED_SECTION_ADDITIONAL_ACTIONS = "additional_actions"


class NextBestActionItem(BaseModel):
    """One recommended action, with the signals that produced it attached.

    `tier` doubles as this recommendation's reason code. The nine values are a closed
    vocabulary and each one names the signal the action came from, so a separate `reason_code`
    field would be the same information under a second name -- which is how a codebase ends up
    with two vocabularies that can disagree.
    """

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
    # low | moderate | high -- how far this conclusion can be leaned on, never a percentage.
    # The same three bands, and the same "a claim is not proof" reasoning, as career_strategy's
    # bottleneck confidence. Deliberately independent of priority: a tier-1 critical role gap
    # is the most important thing to act on *and* the least certain, because it is inferred
    # from evidence we do not have rather than from anything measured.
    confidence: Optional[str] = None
    # Where this action should take the student, drawn from career_strategy's existing
    # destination vocabulary -- the Android app already maps every one of those keys to a real
    # route, so nothing here can point somewhere that does not exist.
    destination: Optional[str] = None


class NextBestActionRead(NextBestActionItem):
    """The endpoint's response: the single top action, plus the ranked remainder.

    Inherits rather than wraps so the body keeps exactly the shape it has always had. An older
    client parses it unchanged and ignores the two new keys.
    """

    # Premium only. Empty for a free student, who still gets the top action above -- the
    # diagnosis is never paywalled, only the depth of the plan.
    additional_actions: List[NextBestActionItem] = []
    locked_sections: List[str] = []
