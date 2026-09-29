"""Orchestration for the guided, staged Project Builder flow. Stage progression is strictly
linear (a stage unlocks only once every earlier stage, by order_index, is completed) -- this
is the actual "guided" part of "guided, staged build flow": the student can't skip ahead to
GitHub README before they've thought through architecture. start_stage/complete_stage are
both idempotent (calling either again on an already-reached state is a no-op, same discipline
as mission_service), so a retried request or a double-tap in the UI can never corrupt state or
error. Completing the final stage automatically flips the build to "completed" and triggers
project_proof_service -- "no separate step," per the architecture plan. Public functions
return Pydantic read schemas directly, same convention as skill_passport_service/
career_twin_service, not raw ORM rows.
"""

import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.ai.provider import AIProvider
from app.models.profile import StudentProfile
from app.models.project_build import ProjectBuild
from app.models.project_build_stage import ProjectBuildStage
from app.schemas.project_builder import (
    GitHubEvidenceRead,
    ProjectBuildListItemRead,
    ProjectBuildRead,
    ProjectBuildStageRead,
    ProjectProofRead,
)
from app.services import github_verification_service, project_proof_service
from app.services.ai_boundary import outside_transaction
from app.services.project_builder.ai_generation import generate_stage_guidance
from app.services.project_builder.stages import STAGE_ORDER, STAGE_TITLES
from app.services.role_context import resolve_role_context

# Safety cap, not a product pagination feature (disclosed gap -- see assessment_service.py's
# _MAX_HISTORY_ROWS for the full rationale): protects response size for a long-lived account.
_MAX_HISTORY_ROWS = 200


class ProjectBuildError(Exception):
    pass


class ProjectBuildNotFoundError(ProjectBuildError):
    pass


class StageNotFoundError(ProjectBuildError):
    pass


class StageLockedError(ProjectBuildError):
    """Raised when a stage is started/completed before every earlier stage (by order_index)
    is completed -- the guided build process is strictly linear."""


class ProofNotReadyError(ProjectBuildError):
    """Raised when proof is requested for a build that hasn't completed its final stage yet
    -- there is nothing generated to return."""


def _stage_read(stage: ProjectBuildStage) -> ProjectBuildStageRead:
    return ProjectBuildStageRead(
        stage_key=stage.stage_key,
        title=STAGE_TITLES[stage.stage_key],
        order_index=stage.order_index,
        status=stage.status,
        guidance_text=stage.guidance_text,
        notes=stage.notes,
        started_at=stage.started_at,
        completed_at=stage.completed_at,
    )


def _list_item_read(build: ProjectBuild) -> ProjectBuildListItemRead:
    return ProjectBuildListItemRead(
        id=build.id,
        title=build.title,
        target_role_name=build.target_role_name,
        status=build.status,
        created_at=build.created_at,
        completed_at=build.completed_at,
    )


def _build_read(build: ProjectBuild, stages: List[ProjectBuildStage]) -> ProjectBuildRead:
    return ProjectBuildRead(
        id=build.id,
        title=build.title,
        target_role_name=build.target_role_name,
        status=build.status,
        created_at=build.created_at,
        completed_at=build.completed_at,
        stack_summary=build.stack_summary,
        stages=[_stage_read(s) for s in sorted(stages, key=lambda s: s.order_index)],
        github_evidence=GitHubEvidenceRead(**build.github_evidence) if build.github_evidence else None,
    )


def create_build(
    db: Session, profile: StudentProfile, title: str, stack_summary: Optional[str], ai_provider: AIProvider
) -> ProjectBuildRead:
    context = resolve_role_context(db, profile)
    role_name = context.display_role_name if context is not None else "this role"
    # Plain values, taken before the boundary below closes the session.
    profile_id = profile.id
    role_family_key = context.role_family_key if context is not None else None
    target_role_name = context.display_role_name if context is not None else None

    # The guidance for all ten stages is generated first, outside any transaction and before
    # anything is written (F12-07). It used to run between the build's own commit and its stages',
    # which both held a transaction across the call and meant a failure could leave a build with
    # no stages in it -- an empty, unusable project the student could not progress.
    with outside_transaction(db):
        guidance = generate_stage_guidance(role_name, title, stack_summary, ai_provider)

    build = ProjectBuild(
        profile_id=profile_id,
        title=title,
        role_family_key=role_family_key,
        target_role_name=target_role_name,
        stack_summary=stack_summary,
        status="not_started",
    )
    db.add(build)
    # flush, not commit: the build and its ten stages are one unit of work.
    db.flush()

    stages = []
    for index, stage_key in enumerate(STAGE_ORDER):
        stage = ProjectBuildStage(
            build_id=build.id,
            stage_key=stage_key,
            order_index=index,
            guidance_text=guidance[stage_key],
        )
        db.add(stage)
        stages.append(stage)
    db.commit()
    for stage in stages:
        db.refresh(stage)
    return _build_read(build, stages)


def list_builds(db: Session, profile_id: int) -> List[ProjectBuildListItemRead]:
    builds = (
        db.query(ProjectBuild)
        .filter(ProjectBuild.profile_id == profile_id)
        .order_by(ProjectBuild.created_at.desc())
        .limit(_MAX_HISTORY_ROWS)
        .all()
    )
    return [_list_item_read(b) for b in builds]


def _get_owned_build(db: Session, profile_id: int, build_id: int) -> ProjectBuild:
    build = db.get(ProjectBuild, build_id)
    if build is None or build.profile_id != profile_id:
        raise ProjectBuildNotFoundError("Project build not found")
    return build


def _get_stages(db: Session, build_id: int) -> List[ProjectBuildStage]:
    return (
        db.query(ProjectBuildStage)
        .filter(ProjectBuildStage.build_id == build_id)
        .order_by(ProjectBuildStage.order_index)
        .all()
    )


def get_build(db: Session, profile_id: int, build_id: int) -> ProjectBuildRead:
    build = _get_owned_build(db, profile_id, build_id)
    stages = _get_stages(db, build.id)
    return _build_read(build, stages)


def _get_stage(stages: List[ProjectBuildStage], stage_key: str) -> ProjectBuildStage:
    for stage in stages:
        if stage.stage_key == stage_key:
            return stage
    raise StageNotFoundError(f"Unknown stage: {stage_key}")


def _is_unlocked(stages: List[ProjectBuildStage], target: ProjectBuildStage) -> bool:
    return all(s.status == "completed" for s in stages if s.order_index < target.order_index)


def start_stage(db: Session, profile: StudentProfile, build_id: int, stage_key: str) -> ProjectBuildStageRead:
    build = _get_owned_build(db, profile.id, build_id)
    stages = _get_stages(db, build.id)
    stage = _get_stage(stages, stage_key)

    if stage.status != "not_started":
        return _stage_read(stage)  # already started or completed -- idempotent no-op

    if not _is_unlocked(stages, stage):
        raise StageLockedError(f"Complete earlier stages before starting {stage_key}")

    stage.status = "in_progress"
    stage.started_at = datetime.datetime.now(datetime.timezone.utc)
    if build.status == "not_started":
        build.status = "in_progress"
    db.commit()
    db.refresh(stage)
    return _stage_read(stage)


def complete_stage(
    db: Session, profile: StudentProfile, build_id: int, stage_key: str, notes: Optional[str], ai_provider: AIProvider
) -> ProjectBuildStageRead:
    build = _get_owned_build(db, profile.id, build_id)
    stages = _get_stages(db, build.id)
    stage = _get_stage(stages, stage_key)

    if stage.status == "completed":
        return _stage_read(stage)  # duplicate completion -- idempotent no-op, never regenerates proof twice

    if not _is_unlocked(stages, stage):
        raise StageLockedError(f"Complete earlier stages before completing {stage_key}")

    stage.status = "completed"
    stage.completed_at = datetime.datetime.now(datetime.timezone.utc)
    if notes is not None:
        stage.notes = notes
    db.commit()
    db.refresh(stage)

    remaining_stages = _get_stages(db, build.id)
    # Materialized before generate_proof, which runs its AI call with no transaction open (F12-07)
    # and therefore detaches `stage`. This response is about the stage the student just completed,
    # so it does not depend on the proof that may be generated below.
    stage_read = _stage_read(stage)
    if all(s.status == "completed" for s in remaining_stages):
        build.status = "completed"
        build.completed_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
        project_proof_service.generate_proof(db, build, remaining_stages, ai_provider)

    return stage_read


def get_proof(db: Session, profile_id: int, build_id: int) -> ProjectProofRead:
    build = _get_owned_build(db, profile_id, build_id)
    if build.status != "completed" or build.proof_defense_questions is None:
        raise ProofNotReadyError("This project isn't finished yet -- complete every stage to generate proof.")
    return ProjectProofRead(
        resume_bullet=build.proof_resume_bullet,
        readme_markdown=build.proof_readme_markdown,
        explanation_60s=build.proof_explanation_60s,
        explanation_5min=build.proof_explanation_5min,
        defense_questions=list(build.proof_defense_questions),
    )


def check_github(db: Session, profile: StudentProfile, build_id: int, repo_url: str) -> GitHubEvidenceRead:
    """Runs a fresh, unauthenticated GitHub check and caches the result on the build --
    github_verification_service.InvalidRepoUrlError/GitHubApiError propagate to the caller
    unchanged (the router maps them to 4xx); a failed or invalid check never partially
    overwrites the previous cached result."""
    build = _get_owned_build(db, profile.id, build_id)
    evidence = github_verification_service.check_repository(repo_url, build.stack_summary)

    build.github_repo_url = repo_url
    build.github_evidence = evidence.model_dump(mode="json")
    db.commit()

    return GitHubEvidenceRead(**build.github_evidence)
