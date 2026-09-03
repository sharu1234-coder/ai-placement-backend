from typing import Dict, List

from sqlalchemy.orm import Session

from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_description import JobDescription
from app.models.roadmap import Roadmap
from app.models.roadmap_task import RoadmapTask
from app.models.role import Role
from app.schemas.progress import ProgressDashboardRead, ProgressTrendPoint
from app.services.weakness_service import compute_weaknesses

_TOP_WEAKNESS_LIMIT = 5


def _assessment_label(
    blueprint: AssessmentBlueprint, jd_by_id: Dict[int, JobDescription], role_by_id: Dict[int, Role]
) -> str:
    if blueprint.jd_id is not None:
        jd = jd_by_id.get(blueprint.jd_id)
        if jd is not None and jd.role_title:
            return f"Test — {jd.role_title}"
    if blueprint.role_id is not None:
        role = role_by_id.get(blueprint.role_id)
        if role is not None:
            return f"Test — {role.name}"
    return "Test"


def _interview_label(interview: Interview) -> str:
    return f"{interview.type.capitalize()} interview ({interview.mode})"


def get_dashboard(db: Session, profile_id: int) -> ProgressDashboardRead:
    """Deterministic (non-AI) aggregation over the profile's own history -- same "our own
    logic" philosophy as weakness_service.compute_weaknesses, which this also calls.
    """
    assessment_rows = (
        db.query(AssessmentResult, AssessmentBlueprint)
        .join(Assessment, Assessment.id == AssessmentResult.assessment_id)
        .join(AssessmentBlueprint, AssessmentBlueprint.id == Assessment.blueprint_id)
        .filter(Assessment.profile_id == profile_id)
        .order_by(AssessmentResult.created_at)
        .all()
    )
    interview_rows = (
        db.query(InterviewEvaluation, Interview)
        .join(Interview, Interview.id == InterviewEvaluation.interview_id)
        .filter(Interview.profile_id == profile_id)
        .order_by(InterviewEvaluation.created_at)
        .all()
    )

    jd_ids = {b.jd_id for _, b in assessment_rows if b.jd_id is not None}
    role_ids = {b.role_id for _, b in assessment_rows if b.role_id is not None}
    jd_by_id = (
        {jd.id: jd for jd in db.query(JobDescription).filter(JobDescription.id.in_(jd_ids)).all()}
        if jd_ids
        else {}
    )
    role_by_id = {role.id: role for role in db.query(Role).filter(Role.id.in_(role_ids)).all()} if role_ids else {}

    trend: List[ProgressTrendPoint] = [
        ProgressTrendPoint(
            created_at=result.created_at,
            kind="assessment",
            label=_assessment_label(blueprint, jd_by_id, role_by_id),
            score=result.overall_score,
        )
        for result, blueprint in assessment_rows
    ] + [
        ProgressTrendPoint(
            created_at=evaluation.created_at,
            kind="interview",
            label=_interview_label(interview),
            score=evaluation.overall_score,
        )
        for evaluation, interview in interview_rows
    ]
    trend.sort(key=lambda point: point.created_at)

    # Readiness score: the two most recent signals (latest test, latest interview) --
    # deliberately not an average of everything ever taken, so an early, since-improved-on
    # attempt doesn't keep dragging down a student's current standing.
    latest_assessment_score = assessment_rows[-1][0].overall_score if assessment_rows else None
    latest_interview_score = interview_rows[-1][0].overall_score if interview_rows else None
    recent_scores = [s for s in (latest_assessment_score, latest_interview_score) if s is not None]
    readiness_score = sum(recent_scores) / len(recent_scores) if recent_scores else None

    active_roadmaps = db.query(Roadmap).filter(Roadmap.profile_id == profile_id, Roadmap.status == "active").all()
    roadmap_ids = [r.id for r in active_roadmaps]
    roadmap_tasks = (
        db.query(RoadmapTask).filter(RoadmapTask.roadmap_id.in_(roadmap_ids)).all() if roadmap_ids else []
    )

    top_weaknesses = [w.skill_tag for w in compute_weaknesses(db, profile_id, jd_id=None)][:_TOP_WEAKNESS_LIMIT]

    return ProgressDashboardRead(
        readiness_score=readiness_score,
        completed_assessments=len(assessment_rows),
        completed_interviews=len(interview_rows),
        roadmap_tasks_done=sum(1 for t in roadmap_tasks if t.status == "done"),
        roadmap_tasks_total=len(roadmap_tasks),
        top_weaknesses=top_weaknesses,
        trend=trend,
    )
