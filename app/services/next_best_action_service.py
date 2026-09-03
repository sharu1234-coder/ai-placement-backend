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
"""

from typing import List, Optional, Set

from sqlalchemy.orm import Session

from app.models.career_path import CareerPath
from app.models.career_path_project import CareerPathProject
from app.models.job_description import JobDescription
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.profile import StudentProfile
from app.schemas.next_best_action import NextBestActionRead
from app.services import evidence_engine
from app.services.role_context import resolve_role_context
from app.services.weakness_service import WeaknessItem, compute_weaknesses

_CORE_WEIGHT_THRESHOLD = 1.0
_SEVERITY_RANK = {"medium": 0, "high": 1}

_ESTIMATED_MINUTES = {
    "learn": 15,
    "practice": 10,
    "project": 45,
    "resume": 10,
    "roadmap": 20,
    "maintenance": 10,
}


def _make(
    tier: Optional[str],
    title: str,
    action_type: str,
    reasons: List[str],
    skill: Optional[str] = None,
    has_recommendation: bool = True,
) -> NextBestActionRead:
    return NextBestActionRead(
        has_recommendation=has_recommendation,
        tier=tier,
        title=title,
        action_type=action_type,
        estimated_minutes=_ESTIMATED_MINUTES[action_type],
        reasons=reasons,
        skill=skill,
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


def get_next_best_action(db: Session, profile: StudentProfile) -> NextBestActionRead:
    context = resolve_role_context(db, profile)
    if context is None or not context.weights:
        return _make(
            None,
            "Select a target role to get a personalized next action.",
            "maintenance",
            ["No target role is set yet."],
            has_recommendation=False,
        )

    skills = list(context.weights.keys())
    evidence_by_skill = {v.skill: v for v in evidence_engine.get_all_skill_evidence(db, profile.id, skills)}

    # Tier 1 -- critical role gap: a core skill (role-weighted >= 1.0) with zero real evidence.
    core_gaps = sorted(
        skill for skill in skills
        if context.weights[skill] >= _CORE_WEIGHT_THRESHOLD and evidence_by_skill[skill].state == "claimed"
    )
    if core_gaps:
        skill = core_gaps[0]
        return _make(
            "critical_role_gap",
            f"Learn {skill} fundamentals",
            "learn",
            [
                f"{context.display_role_name} is your current target role.",
                f"{skill} is a core skill for this role.",
                "You have no evidence yet for this skill.",
            ],
            skill=skill,
        )

    weaknesses = _compute_all_weaknesses(db, profile.id)

    # Tier 2 -- repeated weakness: confirmed by 2+ independent signal sources.
    repeated = [w for w in weaknesses if len(w.sources) >= 2]
    if repeated:
        w = repeated[0]
        return _make(
            "repeated_weakness",
            f"Fix {w.skill_tag}",
            "practice",
            [f"{w.skill_tag} was flagged by multiple signals: {', '.join(sorted(w.sources))}."],
            skill=w.skill_tag,
        )

    # Tier 3 -- assessment weakness.
    assessment_only = _by_sources(weaknesses, {"test_result"})
    if assessment_only:
        w = assessment_only[0]
        return _make(
            "assessment_weakness",
            f"Practice {w.skill_tag}",
            "practice",
            [f"Your last assessment flagged {w.skill_tag} as a weak area."],
            skill=w.skill_tag,
        )

    # Tier 4 -- interview weakness.
    interview_only = _by_sources(weaknesses, {"interview_eval"})
    if interview_only:
        w = interview_only[0]
        label = w.skill_tag.replace("interview:", "").replace("_", " ")
        return _make(
            "interview_weakness",
            f"Practice interview answers on {label}",
            "practice",
            [f"A recent interview evaluation flagged {label} as a weak area."],
            skill=w.skill_tag,
        )

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
            return _make(
                "project_requirement",
                f"Work on: {pending_project.title}",
                "project",
                [f"{pending_project.title} is part of your Career Path and isn't finished yet."],
            )

    # Tier 6 -- resume/JD gap.
    jd_only = _by_sources(weaknesses, {"jd_gap"})
    if jd_only:
        w = jd_only[0]
        return _make(
            "resume_jd_gap",
            f"Close the {w.skill_tag} gap for your job description",
            "resume",
            [f"A job description you're preparing for requires {w.skill_tag}, which isn't on your resume yet."],
            skill=w.skill_tag,
        )

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
            return _make(
                "roadmap_dependency",
                f"Continue your roadmap: {next_topic.title}",
                "roadmap",
                ["Your roadmap has an unstarted topic ready to go."],
                skill=next_topic.skill_tag,
            )

    # Tier 8 -- stale skill: real evidence, but old.
    stale_skills = sorted(skill for skill in skills if evidence_by_skill[skill].state == "stale")
    if stale_skills:
        skill = stale_skills[0]
        return _make(
            "stale_skill",
            f"Refresh your {skill} evidence",
            "practice",
            [f"Your {skill} evidence hasn't been demonstrated recently."],
            skill=skill,
        )

    # Tier 9 -- maintenance: nothing urgent. Keep the currently-weakest-but-fine skill sharp
    # rather than fabricating a gap that doesn't exist.
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
