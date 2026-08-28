from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.placement_path import GeneratedPlacementPathExplanations
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.resume import Resume
from app.schemas.assessment import BlueprintSection
from app.schemas.placement_path import (
    NextBestActionRead,
    PlacementPathRead,
    PlacementPathStageRead,
    PlacementPathTopicRead,
)
from app.services import placement_path_rules as rules
from app.services.skill_matching import latest_resume_skills
from app.services.weakness_service import compute_weaknesses

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "placement_path_explainer_v1.txt"

# Deliberately stricter than assessment_run_service's 0.6 "weakness" cutoff -- passing a
# roadmap topic should mean solid competence, not merely "not currently a weak spot".
TOPIC_PASS_THRESHOLD = 0.7

# A short, single-skill spot-check via the existing assessment engine, not a full multi-
# section assessment -- practicing one roadmap topic should be quick, not a 20-question test.
PRACTICE_QUESTION_COUNT = 3

_BLUEPRINT_SOURCE = "placement_path"


class NoPlacementRelevantSkillsError(Exception):
    """The resume has no skills matching any recognized placement/interview-prep track."""


class PlacementPathTopicNotFoundError(Exception):
    """Topic not found, or not owned by this profile (via its path)."""


class TopicLockedError(Exception):
    """The topic's prerequisite isn't completed yet."""


class ManualCompletionNotAllowedError(Exception):
    """This topic has a practice path -- it can only be completed via a passing assessment,
    never the manual fallback (the "honest completion" rule)."""


class NoAssessmentPathError(Exception):
    """This topic has no skill to practice -- it's one of the manual-only checkpoint topics."""


def _latest_completed_resume_id(db: Session, profile_id: int) -> Optional[int]:
    resume = (
        db.query(Resume)
        .filter(Resume.profile_id == profile_id, Resume.parse_status == "completed")
        .order_by(Resume.id.desc())
        .first()
    )
    return resume.id if resume is not None else None


def _build_explanation_prompt(specs: List[rules.TopicSpec]) -> str:
    lines = [
        f"{s.topic_key} | {s.title} | {s.track_key} | {s.stage} | {s.skill_tag}" for s in specs
    ]
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(topic_lines="\n".join(lines))


def _fallback_explanation(spec: "rules.TopicSpec") -> str:
    track_label = spec.track_key.replace("_", " ")
    return f"Strengthens your {spec.title} skills -- a core part of your {track_label} placement preparation."


def _generate_explanations(ai_provider: AIProvider, specs: List[rules.TopicSpec]) -> Dict[str, str]:
    """Never blocks path generation: on any AI failure, or a partial/invalid response, the
    affected topics fall back to a deterministic template sentence rather than failing the
    whole generation. AI here is decorative (explanatory text only), never structural."""
    valid_keys = {s.topic_key for s in specs}
    explanations: Dict[str, str] = {}
    try:
        prompt = _build_explanation_prompt(specs)
        generated: GeneratedPlacementPathExplanations = ai_provider.generate_structured(
            prompt, GeneratedPlacementPathExplanations
        )
        for item in generated.explanations:
            if item.topic_key in valid_keys:
                explanations[item.topic_key] = item.why_it_matters
    except AIGenerationError:
        pass

    for spec in specs:
        if spec.topic_key not in explanations:
            explanations[spec.topic_key] = _fallback_explanation(spec)
    return explanations


def _weak_skill_tags(db: Session, profile_id: int) -> frozenset:
    weaknesses = compute_weaknesses(db, profile_id, jd_id=None)
    return frozenset(w.skill_tag for w in weaknesses)


def generate_placement_path(db: Session, profile_id: int, ai_provider: AIProvider) -> PlacementPathRead:
    """Resume skills -> matched tracks -> deterministic topic tree -> batched AI explanations
    (never blocking) -> persist. If an active path already exists it is superseded (never
    deleted), and any topic whose (track_key, topic_key) still exists in the newly built tree
    carries its status/completed_via/practice_blueprint_id forward -- regeneration never
    silently discards progress."""
    resume_skills = latest_resume_skills(db, profile_id)  # propagates NoResumeAnalysisError

    matched = rules.match_domain_tracks(resume_skills)
    languages = rules.detect_primary_languages(resume_skills)
    specs = rules.build_topic_specs(matched, languages)
    if not specs:
        raise NoPlacementRelevantSkillsError("No placement-relevant resume skills found")

    existing_active = (
        db.query(PlacementPath)
        .filter(PlacementPath.profile_id == profile_id, PlacementPath.status == "active")
        .first()
    )
    carry_forward: Dict[Tuple[str, str], PlacementPathTopic] = {}
    if existing_active is not None:
        old_topics = (
            db.query(PlacementPathTopic).filter(PlacementPathTopic.path_id == existing_active.id).all()
        )
        carry_forward = {(t.track_key, t.topic_key): t for t in old_topics}
        existing_active.status = "superseded"
        db.commit()

    explanations = _generate_explanations(ai_provider, specs)

    path = PlacementPath(
        profile_id=profile_id,
        status="active",
        source_resume_id=_latest_completed_resume_id(db, profile_id),
    )
    db.add(path)
    db.commit()
    db.refresh(path)

    for spec in specs:
        carried = carry_forward.get((spec.track_key, spec.topic_key))
        db.add(
            PlacementPathTopic(
                path_id=path.id,
                stage=spec.stage,
                track_key=spec.track_key,
                topic_key=spec.topic_key,
                order_in_track=spec.order_in_track,
                title=spec.title,
                skill_tag=spec.skill_tag,
                why_it_matters=explanations[spec.topic_key],
                practice_blueprint_id=carried.practice_blueprint_id if carried else None,
                status=carried.status if carried else "not_started",
                completed_via=carried.completed_via if carried else None,
            )
        )
    db.commit()

    return _path_read(db, path, profile_id)


def get_current_placement_path(db: Session, profile_id: int) -> Optional[PlacementPathRead]:
    path = (
        db.query(PlacementPath)
        .filter(PlacementPath.profile_id == profile_id, PlacementPath.status == "active")
        .first()
    )
    if path is None:
        return None
    return _path_read(db, path, profile_id)


def get_placement_path_history(db: Session, profile_id: int) -> List[PlacementPathRead]:
    """Every path this profile has ever had (active and superseded), most recent first --
    surfaces the history that generate_placement_path already preserves by never deleting
    superseded rows."""
    paths = (
        db.query(PlacementPath)
        .filter(PlacementPath.profile_id == profile_id)
        .order_by(PlacementPath.created_at.desc())
        .all()
    )
    # Only the active path is live-reconciled against assessment results (matching
    # get_current_placement_path's own behavior) -- a superseded path is a frozen historical
    # snapshot, not something that should keep silently changing shape (and re-running
    # reconciliation's weakness/priority computation for every past path on every history
    # fetch would be wasted work for a value nothing reads).
    return [_path_read(db, path, profile_id, reconcile=(path.status == "active")) for path in paths]


def _get_owned_topic(db: Session, profile_id: int, topic_id: int) -> Tuple[PlacementPathTopic, PlacementPath]:
    topic = db.get(PlacementPathTopic, topic_id)
    if topic is None:
        raise PlacementPathTopicNotFoundError("Topic not found")
    path = db.get(PlacementPath, topic.path_id)
    if path is None or path.profile_id != profile_id:
        raise PlacementPathTopicNotFoundError("Topic not found")
    return topic, path


def _prerequisite(db: Session, topic: PlacementPathTopic) -> Optional[PlacementPathTopic]:
    if topic.order_in_track <= 1:
        return None
    return (
        db.query(PlacementPathTopic)
        .filter(
            PlacementPathTopic.path_id == topic.path_id,
            PlacementPathTopic.track_key == topic.track_key,
            PlacementPathTopic.order_in_track == topic.order_in_track - 1,
        )
        .first()
    )


def _effective_status(db: Session, topic: PlacementPathTopic) -> str:
    prereq = _prerequisite(db, topic)
    return rules.compute_effective_status(topic.status, prereq.status if prereq else None)


def start_topic(db: Session, profile_id: int, topic_id: int) -> PlacementPathTopicRead:
    topic, path = _get_owned_topic(db, profile_id, topic_id)
    if _effective_status(db, topic) == "locked":
        raise TopicLockedError("This topic's prerequisite isn't completed yet")
    if topic.status == "not_started":
        topic.status = "in_progress"
        db.commit()
    return _topic_read(db, topic, profile_id, path)


def get_or_create_practice_blueprint(db: Session, profile_id: int, topic_id: int) -> int:
    topic, path = _get_owned_topic(db, profile_id, topic_id)
    if _effective_status(db, topic) == "locked":
        raise TopicLockedError("This topic's prerequisite isn't completed yet")
    if topic.practice_blueprint_id is not None:
        return topic.practice_blueprint_id

    spec = rules.topic_spec_for(topic.track_key, topic.topic_key)
    if spec is None or spec.assessment_type is None:
        raise NoAssessmentPathError("This topic has no practice path -- use manual completion instead")

    weak_tags = _weak_skill_tags(db, profile_id)
    section = BlueprintSection(
        skill=topic.skill_tag,
        assessment_type=spec.assessment_type,
        weight=1.0,
        question_count=PRACTICE_QUESTION_COUNT,
        is_gap=topic.skill_tag in weak_tags,
    )
    blueprint = AssessmentBlueprint(
        role_id=None,
        jd_id=None,
        profile_id=profile_id,
        source=_BLUEPRINT_SOURCE,
        sections=[section.model_dump()],
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)

    topic.practice_blueprint_id = blueprint.id
    if topic.status == "not_started":
        topic.status = "in_progress"
    db.commit()

    return blueprint.id


def mark_topic_complete_manual(db: Session, profile_id: int, topic_id: int) -> PlacementPathTopicRead:
    topic, path = _get_owned_topic(db, profile_id, topic_id)
    if _effective_status(db, topic) == "locked":
        raise TopicLockedError("This topic's prerequisite isn't completed yet")

    # Gated on the topic's CATALOG definition (does this topic type support practice at
    # all), not merely whether a blueprint has been lazily created yet -- a student must not
    # be able to skip straight to manual completion on a practiceable topic just because they
    # haven't tapped "Practice" yet. Only the 3 manual-only checkpoint stages (no
    # assessment_type in the catalog) ever qualify for this fallback.
    spec = rules.topic_spec_for(topic.track_key, topic.topic_key)
    if spec is not None and spec.assessment_type is not None:
        raise ManualCompletionNotAllowedError(
            "This topic has a practice path -- complete it via a passing assessment instead"
        )

    topic.status = "completed"
    topic.completed_via = "manual"
    db.commit()
    return _topic_read(db, topic, profile_id, path)


def _reconcile_completions(db: Session, profile_id: int, path: PlacementPath) -> None:
    """Read-time reconciliation: for every topic with a practice path that isn't already
    marked completed, check whether the student has a passing AssessmentResult against that
    topic's blueprint (best of any number of retry attempts) and mark it completed if so.
    Zero hooks into assessment_run_service -- this only reads what it already persists."""
    pending = (
        db.query(PlacementPathTopic)
        .filter(
            PlacementPathTopic.path_id == path.id,
            PlacementPathTopic.practice_blueprint_id.isnot(None),
            PlacementPathTopic.status != "completed",
        )
        .all()
    )
    if not pending:
        return

    blueprint_ids = [t.practice_blueprint_id for t in pending]
    best_score_by_blueprint: Dict[int, float] = dict(
        db.query(Assessment.blueprint_id, func.max(AssessmentResult.overall_score))
        .join(AssessmentResult, AssessmentResult.assessment_id == Assessment.id)
        .filter(Assessment.blueprint_id.in_(blueprint_ids), Assessment.profile_id == profile_id)
        .group_by(Assessment.blueprint_id)
        .all()
    )

    changed = False
    for topic in pending:
        best_score = best_score_by_blueprint.get(topic.practice_blueprint_id)
        if best_score is not None and best_score >= TOPIC_PASS_THRESHOLD:
            topic.status = "completed"
            topic.completed_via = "assessment"
            changed = True
    if changed:
        db.commit()


def _topic_read(
    db: Session, topic: PlacementPathTopic, profile_id: int, path: PlacementPath
) -> PlacementPathTopicRead:
    all_topics = db.query(PlacementPathTopic).filter(PlacementPathTopic.path_id == path.id).all()
    prereq_by_id = {t.id: _prerequisite(db, t) for t in all_topics}
    prereq = prereq_by_id[topic.id]
    effective = rules.compute_effective_status(topic.status, prereq.status if prereq else None)

    weak_tags = _weak_skill_tags(db, profile_id)
    priority = _priority_for(topic, all_topics, weak_tags, prereq_by_id)

    return PlacementPathTopicRead(
        id=topic.id,
        stage=topic.stage,
        track_key=topic.track_key,
        topic_key=topic.topic_key,
        title=topic.title,
        why_it_matters=topic.why_it_matters or "",
        skill_tag=topic.skill_tag,
        status=effective,
        priority=priority,
        prerequisite_topic_id=prereq.id if prereq else None,
        has_practice=rules.topic_spec_for(topic.track_key, topic.topic_key) is not None
        and rules.topic_spec_for(topic.track_key, topic.topic_key).assessment_type is not None,
        completed_via=topic.completed_via,
    )


def _priority_for(
    topic: PlacementPathTopic,
    all_topics: List[PlacementPathTopic],
    weak_tags: frozenset,
    prereq_by_id: Dict[int, Optional[PlacementPathTopic]],
) -> str:
    track_topics = sorted((t for t in all_topics if t.track_key == topic.track_key), key=lambda t: t.order_in_track)
    is_first_incomplete = False
    for t in track_topics:
        t_prereq = prereq_by_id.get(t.id)
        t_effective = rules.compute_effective_status(t.status, t_prereq.status if t_prereq else None)
        if t_effective in ("not_started", "in_progress"):
            is_first_incomplete = t.id == topic.id
            break

    prereq = prereq_by_id.get(topic.id)
    view = rules.TopicView(
        topic_key=topic.topic_key,
        track_key=topic.track_key,
        stage=topic.stage,
        order_in_track=topic.order_in_track,
        title=topic.title,
        skill_tag=topic.skill_tag,
        status=topic.status,
        prerequisite_status=prereq.status if prereq else None,
        global_order=0,  # unused by compute_topic_priority
    )
    return rules.compute_topic_priority(view, is_first_incomplete, len(track_topics), weak_tags)


def _path_read(db: Session, path: PlacementPath, profile_id: int, reconcile: bool = True) -> PlacementPathRead:
    if reconcile:
        _reconcile_completions(db, profile_id, path)

    topics = db.query(PlacementPathTopic).filter(PlacementPathTopic.path_id == path.id).all()
    weak_tags = _weak_skill_tags(db, profile_id)

    views: List[rules.TopicView] = []
    priorities: Dict[str, str] = {}
    reads_by_id: Dict[int, PlacementPathTopicRead] = {}
    prereq_by_id: Dict[int, Optional[PlacementPathTopic]] = {t.id: _prerequisite(db, t) for t in topics}

    for topic in topics:
        prereq = prereq_by_id[topic.id]
        priority = _priority_for(topic, topics, weak_tags, prereq_by_id)
        priorities[topic.topic_key] = priority
        views.append(
            rules.TopicView(
                topic_key=topic.topic_key,
                track_key=topic.track_key,
                stage=topic.stage,
                order_in_track=topic.order_in_track,
                title=topic.title,
                skill_tag=topic.skill_tag,
                status=topic.status,
                prerequisite_status=prereq.status if prereq else None,
                global_order=rules.track_rank(topic.track_key) * 1000 + topic.order_in_track,
            )
        )
        spec = rules.topic_spec_for(topic.track_key, topic.topic_key)
        reads_by_id[topic.id] = PlacementPathTopicRead(
            id=topic.id,
            stage=topic.stage,
            track_key=topic.track_key,
            topic_key=topic.topic_key,
            title=topic.title,
            why_it_matters=topic.why_it_matters or "",
            skill_tag=topic.skill_tag,
            status=rules.compute_effective_status(topic.status, prereq.status if prereq else None),
            priority=priority,
            prerequisite_topic_id=prereq.id if prereq else None,
            has_practice=spec is not None and spec.assessment_type is not None,
            completed_via=topic.completed_via,
        )

    stages: List[PlacementPathStageRead] = []
    for stage_key in rules.STAGE_ORDER:
        stage_rows = [t for t in topics if t.stage == stage_key]
        if not stage_rows:
            continue
        # Sort the DB rows (which carry order_in_track) rather than the read schema -- topic_key
        # is a free-text slug, not a sortable position, so sorting by it directly would put
        # e.g. "sql_aggregation" (order 3) ahead of "sql_basics" (order 1) alphabetically.
        stage_rows.sort(key=lambda t: (rules.track_rank(t.track_key), t.order_in_track))
        stage_topics = [reads_by_id[t.id] for t in stage_rows]
        stages.append(PlacementPathStageRead(stage=stage_key, topics=stage_topics))

    completed_count, total_count = rules.compute_progress(views)

    next_action_spec = rules.pick_next_best_action(views, priorities)
    next_action: Optional[NextBestActionRead] = None
    if next_action_spec is not None:
        matching_topic = next(t for t in topics if t.topic_key == next_action_spec.topic_key)
        next_action = NextBestActionRead(
            topic_id=matching_topic.id, title=next_action_spec.title, reason=next_action_spec.reason
        )

    return PlacementPathRead(
        id=path.id,
        status=path.status,
        created_at=path.created_at,
        completed_topics=completed_count,
        total_topics=total_count,
        stages=stages,
        next_best_action=next_action,
    )
