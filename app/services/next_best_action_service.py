"""Next Best Action -- the single most valuable thing a student should do next, ranked by a
fixed, documented priority order (critical role gap > repeated weakness > assessment weakness
> interview weakness > project requirement > resume/JD gap > roadmap dependency > stale skill
> maintenance -- the exact order the product spec calls for). Adds no new persisted state:
this is a pure ranking function over role_context, evidence_engine, and weakness_service, all
already shared by Career Twin/Skill Passport. `reasons` is always built from the same signals
that drove the decision, in Python, before returning -- never asked of the AI after the fact --
so a recommendation is explainable by construction, never a black box.

Tier 2 ("repeated weakness") is a real, if provisional, signal: a weakness confirmed by 2+
independent sources (e.g. both a JD gap and a test result) already existing in
weakness_service's output. A precise cross-session occurrence counter arrives later with
Interview Memory; until then this tier never fabricates a count it doesn't have.

The ladder yields a short ranked list, not only its winner. Each tier contributes at most one
action, so three recommendations are three different kinds of work rather than the same tier
repeated -- and `maintenance` is offered only when no tier above it had anything to say, since
"no urgent gaps detected" is false the moment one exists. Duplicates are removed by skill: SQL
arriving from a role gap and from a weakness is one SQL action, not two.
"""

from typing import List, Optional, Set

from sqlalchemy.orm import Session

from app.models.career_path import CareerPath
from app.models.career_path_project import CareerPathProject
from app.models.job_description import JobDescription
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.profile import StudentProfile
from app.schemas.next_best_action import NextBestActionItem, NextBestActionRead
from app.services import application_outcome, evidence_engine
from app.services.role_context import resolve_role_context
from app.services.weakness_service import WeaknessItem, compute_weaknesses

_CORE_WEIGHT_THRESHOLD = 1.0
_SEVERITY_RANK = {"medium": 0, "high": 1}

# Three is the product's own answer to "how many things should someone do next". A longer list
# is not a plan, and the tiers below it are lower-value by construction.
MAX_ACTIONS = 3

_ESTIMATED_MINUTES = {
    "learn": 15,
    "practice": 10,
    "project": 45,
    "resume": 10,
    "roadmap": 20,
    "maintenance": 10,
}

# How far each tier's conclusion can be leaned on -- the same three bands career_strategy's
# bottleneck uses. Deliberately NOT a restatement of priority: tier 1 is simultaneously the most
# important thing to act on and the least certain, because it is inferred from evidence we do
# not have rather than from anything measured. Saying so is the difference between "this matters
# most" and "we know this about you".
_CONFIDENCE_BY_TIER = {
    "critical_role_gap": "low",  # inferred from absent evidence
    "repeated_weakness": "high",  # two or more independent signals agree
    "assessment_weakness": "moderate",  # one measured signal
    "interview_weakness": "moderate",  # one measured signal
    "project_requirement": "high",  # a recorded, unfinished item: a fact, not an inference
    "resume_jd_gap": "low",  # inferred from an absence on the resume
    "roadmap_dependency": "high",  # a recorded, unstarted topic
    "stale_skill": "moderate",  # real evidence, only old
    "maintenance": "low",  # nothing urgent to point at
}

# Where each tier sends the student, in career_strategy's existing destination vocabulary.
# Every key here is one the Android app's own destination table already resolves to a real
# route, so a recommendation cannot point at a screen that does not exist.
_DESTINATION_BY_TIER = {
    "critical_role_gap": "roadmap",
    "repeated_weakness": "practice",
    "assessment_weakness": "practice",
    "interview_weakness": "interview",
    "project_requirement": "career_path",
    "resume_jd_gap": "resume_ats",
    "roadmap_dependency": "roadmap",
    "stale_skill": "practice",
    "maintenance": "practice",
}

# Said only when GitHub genuinely is not connected. "You have no evidence for this" and "we
# never looked at the one place your evidence would be" are different statements, and only the
# first is a finding -- see evidence_engine.unavailable_sources.
_EXTERNAL_UNAVAILABLE_REASON = (
    "GitHub isn't connected, so any public code you have isn't being counted yet."
)


def _make(
    tier: Optional[str],
    title: str,
    action_type: str,
    reasons: List[str],
    skill: Optional[str] = None,
    has_recommendation: bool = True,
) -> NextBestActionItem:
    return NextBestActionItem(
        has_recommendation=has_recommendation,
        tier=tier,
        title=title,
        action_type=action_type,
        estimated_minutes=_ESTIMATED_MINUTES[action_type],
        reasons=reasons,
        skill=skill,
        confidence=_CONFIDENCE_BY_TIER.get(tier) if tier else None,
        destination=_DESTINATION_BY_TIER.get(tier) if tier else None,
    )


def _by_sources(weaknesses: List[WeaknessItem], sources: Set[str]) -> List[WeaknessItem]:
    return [w for w in weaknesses if set(w.sources) == sources]


def _merge_weaknesses(*weakness_lists: List[WeaknessItem]) -> List[WeaknessItem]:
    """compute_weaknesses() is scoped to a single jd_id at a time (role-only via jd_id=None,
    or one specific JD) -- it never merges JD-derived gaps into a role-based call. Next Best
    Action needs both together (a JD gap must be able to count toward "repeated weakness" and
    power tier 6 on its own), so this merges multiple calls' results the same way
    weakness_service's own internal _upsert does: union sources per skill_tag, escalate to the
    higher severity, never silently drop a signal."""
    merged: dict = {}
    for weaknesses in weakness_lists:
        for w in weaknesses:
            existing = merged.get(w.skill_tag)
            if existing is None:
                merged[w.skill_tag] = WeaknessItem(skill_tag=w.skill_tag, severity=w.severity, sources=list(w.sources))
                continue
            for source in w.sources:
                if source not in existing.sources:
                    existing.sources.append(source)
            if _SEVERITY_RANK[w.severity] > _SEVERITY_RANK[existing.severity]:
                existing.severity = w.severity
    return sorted(merged.values(), key=lambda w: (-_SEVERITY_RANK[w.severity], w.skill_tag))


def _compute_all_weaknesses(db: Session, profile_id: int) -> List[WeaknessItem]:
    role_weaknesses = compute_weaknesses(db, profile_id, jd_id=None)
    recent_jd = (
        db.query(JobDescription).filter(JobDescription.profile_id == profile_id).order_by(JobDescription.id.desc()).first()
    )
    jd_weaknesses = compute_weaknesses(db, profile_id, jd_id=recent_jd.id) if recent_jd is not None else []
    return _merge_weaknesses(role_weaknesses, jd_weaknesses)


def _no_role_action() -> NextBestActionItem:
    """What to say before there is a target role to measure anything against.

    has_recommendation=False, because "choose a role" is a prerequisite rather than career
    work -- the client renders it differently, and a fabricated skill action here would be
    advice derived from nothing.
    """
    return _make(
        None,
        "Select a target role to get a personalized next action.",
        "maintenance",
        ["No target role is set yet."],
        has_recommendation=False,
    )


def _build_candidates(
    db: Session,
    profile: StudentProfile,
    context,
    evidence_by_skill: dict,
    skills: List[str],
    limit: int,
) -> List[NextBestActionItem]:
    """Tiers 1-8 in priority order, at most one action per tier, de-duplicated as they arrive.

    Tier 9 (maintenance) is deliberately not built here: it states that no urgent gaps were
    detected, which is only true when this list comes back empty. get_actions adds it in that
    case and in no other.

    Stops querying as soon as `limit` distinct actions exist. That is what keeps the
    single-action callers -- Career Strategy's bottleneck and Today's Mission -- reading
    exactly the rows they read before this list existed: a tier-1 hit still returns without
    ever touching Career Path projects or Placement Path topics.
    """
    candidates: List[NextBestActionItem] = []
    seen: Set[str] = set()

    def add(action: NextBestActionItem) -> None:
        """One action per subject, highest tier first.

        SQL arriving as a role gap, as a repeated weakness and as a JD gap is one SQL action:
        three cards naming the same skill read as a broken screen, and the tier that found it
        first is by construction the most valuable framing of it. Keyed on the skill where
        there is one and otherwise on the title, so the two tiers that recommend a named piece
        of work (a Career Path project, a roadmap topic) cannot collapse into each other.
        """
        key = (action.skill or action.title).strip().lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(action)

    def full() -> bool:
        return len(candidates) >= limit

    # Tier 1 -- critical role gap: a core skill (role-weighted >= 1.0) with zero real evidence.
    core_gaps = sorted(
        skill for skill in skills
        if context.weights[skill] >= _CORE_WEIGHT_THRESHOLD and evidence_by_skill[skill].state == "claimed"
    )
    if core_gaps:
        skill = core_gaps[0]
        reasons = [
            f"{context.display_role_name} is your current target role.",
            f"{skill} is a core skill for this role.",
            "You have no evidence yet for this skill.",
        ]
        # The one tier whose entire case is an absence of evidence, and therefore the one tier
        # obliged to say whether we could see everywhere. Asked only when there is something to
        # qualify, so the extra read never lands on the paths that do not need it.
        if evidence_engine.SOURCE_EXTERNAL in evidence_engine.unavailable_sources(db, profile.id):
            reasons.append(_EXTERNAL_UNAVAILABLE_REASON)
        add(
            _make("critical_role_gap", f"Learn {skill} fundamentals", "learn", reasons, skill=skill)
        )

    if full():
        return candidates

    weaknesses = _compute_all_weaknesses(db, profile.id)

    # Tier 2 -- repeated weakness: confirmed by 2+ independent signal sources.
    repeated = [w for w in weaknesses if len(w.sources) >= 2]
    if repeated:
        w = repeated[0]
        add(
            _make(
                "repeated_weakness",
                f"Fix {w.skill_tag}",
                "practice",
                [f"{w.skill_tag} was flagged by multiple signals: {', '.join(sorted(w.sources))}."],
                skill=w.skill_tag,
            )
        )

    # Tier 3 -- assessment weakness.
    assessment_only = _by_sources(weaknesses, {"test_result"})
    if assessment_only:
        w = assessment_only[0]
        add(
            _make(
                "assessment_weakness",
                f"Practice {w.skill_tag}",
                "practice",
                [f"Your last assessment flagged {w.skill_tag} as a weak area."],
                skill=w.skill_tag,
            )
        )

    # Tier 4 -- interview weakness.
    interview_only = _by_sources(weaknesses, {"interview_eval"})
    if interview_only:
        w = interview_only[0]
        label = w.skill_tag.replace("interview:", "").replace("_", " ")
        add(
            _make(
                "interview_weakness",
                f"Practice interview answers on {label}",
                "practice",
                [f"A recent interview evaluation flagged {label} as a weak area."],
                skill=w.skill_tag,
            )
        )

    if full():
        return candidates

    # Tier 5 -- project requirement: an incomplete Career Path project.
    path = db.query(CareerPath).filter(CareerPath.profile_id == profile.id, CareerPath.is_primary.is_(True)).first()
    if path is not None:
        pending_project = (
            db.query(CareerPathProject)
            .filter(CareerPathProject.career_path_id == path.id, CareerPathProject.status != "completed")
            .order_by(CareerPathProject.order_index)
            .first()
        )
        if pending_project is not None:
            add(
                _make(
                    "project_requirement",
                    f"Work on: {pending_project.title}",
                    "project",
                    [f"{pending_project.title} is part of your Career Path and isn't finished yet."],
                )
            )

    # Tier 6 -- resume/JD gap.
    jd_only = _by_sources(weaknesses, {"jd_gap"})
    if jd_only:
        w = jd_only[0]
        add(
            _make(
                "resume_jd_gap",
                f"Close the {w.skill_tag} gap for your job description",
                "resume",
                [f"A job description you're preparing for requires {w.skill_tag}, which isn't on your resume yet."],
                skill=w.skill_tag,
            )
        )

    if full():
        return candidates

    # Tier 7 -- roadmap dependency: the next not-started Placement Path topic.
    placement_path = (
        db.query(PlacementPath).filter(PlacementPath.profile_id == profile.id, PlacementPath.status == "active").first()
    )
    if placement_path is not None:
        next_topic = (
            db.query(PlacementPathTopic)
            .filter(PlacementPathTopic.path_id == placement_path.id, PlacementPathTopic.status == "not_started")
            .order_by(PlacementPathTopic.order_in_track)
            .first()
        )
        if next_topic is not None:
            add(
                _make(
                    "roadmap_dependency",
                    f"Continue your roadmap: {next_topic.title}",
                    "roadmap",
                    ["Your roadmap has an unstarted topic ready to go."],
                    skill=next_topic.skill_tag,
                )
            )

    # Tier 8 -- stale skill: real evidence, but old.
    stale_skills = sorted(skill for skill in skills if evidence_by_skill[skill].state == "stale")
    if stale_skills:
        skill = stale_skills[0]
        add(
            _make(
                "stale_skill",
                f"Refresh your {skill} evidence",
                "practice",
                [f"Your {skill} evidence hasn't been demonstrated recently."],
                skill=skill,
            )
        )

    return candidates


def _live_interview_action(applications) -> Optional[NextBestActionItem]:
    """The maintenance fallback, when a real application is sitting at the interview stage.

    This is the ONLY place a real-world application changes what gets recommended, and it is
    deliberately confined to the fallback -- the point at which the ranked tiers above have
    found nothing urgent and the alternative is "keep a skill sharp". Every measured tier still
    outranks it, because a recorded weakness is a better reason to act than a calendar event.

    What it must never become: a causal claim. It says an interview stage is live and that
    interview practice is therefore the more useful of two non-urgent options. It does NOT say
    the student is weak, that they will fail, or that any particular skill is why -- none of
    which this app can know from a tracker status. The reason line is phrased as the fact it is.

    Reuses the existing interview_weakness destination and confidence band rather than adding a
    tier: no new vocabulary means the Android destination table already resolves it, and no new
    confidence band means nothing has to decide what this tier's certainty means.
    """
    if applications is None or applications.at_interview_stage <= 0:
        return None
    return _make(
        "interview_weakness",
        "Run a mock interview before your live one",
        "practice",
        [
            # A fact from the student's own tracker, stated as a fact. Plural handled because
            # "1 applications" reads as a bug and undermines trust in everything beside it.
            f"You have {applications.at_interview_stage} application"
            f"{'s' if applications.at_interview_stage > 1 else ''} at the interview stage."
        ],
    )


def _maintenance_action(skills: List[str], evidence_by_skill: dict) -> NextBestActionItem:
    """Tier 9 -- nothing urgent. Keep the currently-weakest-but-fine skill sharp rather than
    fabricating a gap that doesn't exist."""
    ranked = sorted(skills, key=lambda s: (evidence_by_skill[s].confidence, s))
    if not ranked:
        return _make(None, "Keep up the momentum", "maintenance", ["No urgent gaps detected."], has_recommendation=False)

    skill = ranked[0]
    return _make(
        "maintenance",
        f"Keep {skill} sharp with a quick practice round",
        "maintenance",
        ["No urgent gaps detected -- this keeps your strongest-need area from going stale."],
        skill=skill,
    )


def get_actions(db: Session, profile: StudentProfile, limit: int = MAX_ACTIONS) -> List[NextBestActionItem]:
    """The ranked shortlist, highest value first. Never empty.

    Deterministic: the same stored rows produce the same list in the same order, every time.
    Every tier is a pure function of data this app already owns, and nothing here calls AI.
    """
    context = resolve_role_context(db, profile)
    if context is None or not context.weights:
        return [_no_role_action()]

    skills = list(context.weights.keys())
    evidence_by_skill = {v.skill: v for v in evidence_engine.get_all_skill_evidence(db, profile.id, skills)}

    actions = _build_candidates(db, profile, context, evidence_by_skill, skills, limit)[:limit]
    if actions:
        return actions
    # Nothing measured is urgent. Only now does a live application get a say, and only to
    # choose between two non-urgent suggestions -- one query, run on the branch that would
    # otherwise return generic maintenance, so the common path costs nothing.
    live = _live_interview_action(application_outcome.context_for_profile(db, profile.id))
    return [live or _maintenance_action(skills, evidence_by_skill)]


def get_next_best_action(db: Session, profile: StudentProfile) -> NextBestActionRead:
    """The single most valuable thing to do next.

    Unchanged in shape and meaning: this is still the top of the same ladder, and Career
    Strategy's bottleneck and Today's Mission both still get exactly the one recommendation
    they already composed with.
    """
    top = get_actions(db, profile, limit=1)[0]
    return NextBestActionRead(**top.model_dump())
