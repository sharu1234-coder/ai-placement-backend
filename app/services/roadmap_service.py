from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
from app.ai.output_guard import leaking_field
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.roadmap import GeneratedRoadmap
from app.models.job_description import JobDescription
from app.models.roadmap import Roadmap
from app.models.roadmap_task import RoadmapTask
from app.schemas.roadmap import RoadmapRead, RoadmapTaskRead
from app.services.ai_boundary import outside_transaction
from app.services.weakness_service import compute_weaknesses

_ROADMAP_PROMPT_PATH = Path(__file__).resolve().parent.parent / "ai" / "prompts" / "roadmap_generator_v2.txt"


class InvalidRoadmapRequestError(Exception):
    """A jd_id that doesn't resolve, or doesn't belong to this profile."""


class NoWeaknessDataError(Exception):
    """No JD skill gaps, assessment results, or interview evaluations exist yet for this
    profile in this scope -- there is nothing to build a roadmap from."""


class RoadmapNotFoundError(Exception):
    """Roadmap not found, or not owned by this profile."""


class RoadmapTaskNotFoundError(Exception):
    """Task not found, or its roadmap isn't owned by this profile."""


def _roadmap_read(db: Session, roadmap: Roadmap) -> RoadmapRead:
    tasks = (
        db.query(RoadmapTask)
        .filter(RoadmapTask.roadmap_id == roadmap.id)
        .order_by(RoadmapTask.day_number, RoadmapTask.id)
        .all()
    )
    return RoadmapRead(
        id=roadmap.id,
        jd_id=roadmap.jd_id,
        duration_days=roadmap.duration_days,
        status=roadmap.status,
        tasks=[
            RoadmapTaskRead(
                id=t.id,
                day_number=t.day_number,
                task_type=t.task_type,
                skill_tag=t.skill_tag,
                title=t.title,
                description=t.description,
                status=t.status,
                due_date=t.due_date,
            )
            for t in tasks
        ],
    )


def _build_prompt(weaknesses, duration_days: int) -> str:
    template = _ROADMAP_PROMPT_PATH.read_text(encoding="utf-8")
    weaknesses_text = "\n".join(
        f"- {w.skill_tag} (severity: {w.severity}, source: {', '.join(w.sources)})" for w in weaknesses
    )
    return render_prompt(template, weaknesses=weaknesses_text, duration_days=duration_days)


def generate_roadmap(
    db: Session, profile_id: int, jd_id: Optional[int], duration_days: int, ai_provider: AIProvider
) -> RoadmapRead:
    if jd_id is not None:
        jd = db.get(JobDescription, jd_id)
        if jd is None or jd.profile_id != profile_id:
            raise InvalidRoadmapRequestError("Job description not found")

    weaknesses = compute_weaknesses(db, profile_id, jd_id)
    if not weaknesses:
        raise NoWeaknessDataError(
            "No test results, interview evaluations, or JD skill gaps yet -- complete a "
            "test or interview (or paste a JD and analyze it) before generating a roadmap."
        )

    prompt = _build_prompt(weaknesses, duration_days)
    # Outside any transaction (F12-07). The prompt is already a plain string and the weaknesses
    # were only needed to build it, so nothing here wants the session.
    with outside_transaction(db):
        generated: GeneratedRoadmap = ai_provider.generate_structured(prompt, GeneratedRoadmap)
        # Before the supersede below, not after. Every task's title, description and skill_tag is
        # generated text that becomes a RoadmapTask row, and superseding first would mean a leaked
        # generation retired the student's working roadmap on its way to being rejected.
        #
        # Raised rather than substituted: a roadmap is a plan for weeks of the student's time, and
        # there is no honest template to put in its place. app/api/roadmap.py already turns this
        # into the provider-agnostic 422 and the student can regenerate.
        leaked = leaking_field(generated)
        if leaked is not None:
            raise AIGenerationError(f"Roadmap output leaked prompt scaffolding in field {leaked}")

    # Re-validated on this side of the boundary, not trusted from before the AI call: the roadmap
    # row below carries a foreign key to the JD, which the student could have deleted while the
    # model was working.
    if jd_id is not None:
        jd = db.get(JobDescription, jd_id)
        if jd is None or jd.profile_id != profile_id:
            raise InvalidRoadmapRequestError("Job description not found")

    # Only one active roadmap per profile+jd scope at a time -- generating a new one
    # (including via reassess, which calls this) supersedes whatever was active before.
    previously_active = (
        db.query(Roadmap)
        .filter(Roadmap.profile_id == profile_id, Roadmap.jd_id == jd_id, Roadmap.status == "active")
        .all()
    )
    for old in previously_active:
        old.status = "superseded"

    roadmap = Roadmap(profile_id=profile_id, jd_id=jd_id, duration_days=duration_days, status="active")
    db.add(roadmap)
    # flush, not commit: the tasks below need the roadmap's id, but retiring the old roadmap,
    # creating the new one and writing its tasks are one unit of work. Three separate commits
    # (which is what this was) could leave an active roadmap with no tasks in it, or supersede the
    # student's working roadmap with nothing to replace it.
    db.flush()

    today = date.today()
    tasks = [
        RoadmapTask(
            roadmap_id=roadmap.id,
            day_number=t.day,
            task_type=t.task_type,
            skill_tag=t.skill_tag,
            title=t.title,
            description=t.description,
            status="pending",
            due_date=today + timedelta(days=max(t.day - 1, 0)),
        )
        for t in generated.tasks
    ]
    db.add_all(tasks)
    db.commit()

    return _roadmap_read(db, roadmap)


def read_roadmap(db: Session, profile_id: int, roadmap_id: int) -> Optional[RoadmapRead]:
    """A specific roadmap of this student's, or None.

    Answers a replayed generate from the roadmap the first attempt produced, so a lost response
    costs neither a second roadmap nor a second AI generation -- which for this endpoint is
    minutes of inference and one of the student's hourly allowance. Ownership is re-checked
    here rather than inferred from the claim.
    """
    roadmap = db.get(Roadmap, roadmap_id)
    if roadmap is None or roadmap.profile_id != profile_id:
        return None
    return _roadmap_read(db, roadmap)


def get_current_roadmap(db: Session, profile_id: int, jd_id: Optional[int]) -> Optional[RoadmapRead]:
    roadmap = (
        db.query(Roadmap)
        .filter(Roadmap.profile_id == profile_id, Roadmap.jd_id == jd_id, Roadmap.status == "active")
        .order_by(Roadmap.id.desc())
        .first()
    )
    return _roadmap_read(db, roadmap) if roadmap is not None else None


def update_task_status(db: Session, profile_id: int, task_id: int, status: str) -> RoadmapTaskRead:
    task = db.get(RoadmapTask, task_id)
    if task is None:
        raise RoadmapTaskNotFoundError("Task not found")
    roadmap = db.get(Roadmap, task.roadmap_id)
    if roadmap is None or roadmap.profile_id != profile_id:
        raise RoadmapTaskNotFoundError("Task not found")

    task.status = status
    db.commit()
    return RoadmapTaskRead(
        id=task.id,
        day_number=task.day_number,
        task_type=task.task_type,
        skill_tag=task.skill_tag,
        title=task.title,
        description=task.description,
        status=task.status,
        due_date=task.due_date,
    )


def reassess_roadmap(db: Session, profile_id: int, roadmap_id: int, ai_provider: AIProvider) -> RoadmapRead:
    roadmap = db.get(Roadmap, roadmap_id)
    if roadmap is None or roadmap.profile_id != profile_id:
        raise RoadmapNotFoundError("Roadmap not found")

    # Regenerate from current weaknesses rather than patching remaining tasks in place --
    # simpler and safer (no risk of a half-updated task list), same jd_id/duration_days.
    # generate_roadmap() supersedes this roadmap as part of creating the new one.
    return generate_roadmap(db, profile_id, roadmap.jd_id, roadmap.duration_days, ai_provider)
