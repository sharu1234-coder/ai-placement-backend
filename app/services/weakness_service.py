from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_match import JobMatch
from app.models.skill_gap import SkillGap

# Below this, an interview evaluation dimension counts as a weakness worth putting on the
# roadmap (same threshold assessment_run_service uses for priority_weaknesses). Public (no
# leading underscore) because weakness_record_service also needs these to decide, per
# dimension, whether a just-completed interview's score is itself a weak or a good signal --
# the two modules must never drift onto different thresholds for what counts as "weak".
INTERVIEW_DIMENSION_THRESHOLD = 0.6
_INTERVIEW_HIGH_SEVERITY_THRESHOLD = 0.4

INTERVIEW_DIMENSIONS = [
    "technical_correctness",
    "relevance",
    "completeness",
    "reasoning",
    "structure",
    "clarity",
    "role_jd_coverage",
]

_SEVERITY_RANK = {"medium": 0, "high": 1}


class WeaknessItem(BaseModel):
    skill_tag: str
    severity: str  # high/medium
    sources: List[str]


def _upsert(weaknesses: dict, skill_tag: str, severity: str, source: str) -> None:
    existing = weaknesses.get(skill_tag)
    if existing is None:
        weaknesses[skill_tag] = WeaknessItem(skill_tag=skill_tag, severity=severity, sources=[source])
        return
    if source not in existing.sources:
        existing.sources.append(source)
    if _SEVERITY_RANK[severity] > _SEVERITY_RANK[existing.severity]:
        existing.severity = severity


def _from_jd_skill_gaps(db: Session, profile_id: int, jd_id: int, weaknesses: dict) -> None:
    job_match = (
        db.query(JobMatch)
        .filter(JobMatch.jd_id == jd_id, JobMatch.profile_id == profile_id)
        .order_by(JobMatch.id.desc())
        .first()
    )
    if job_match is None:
        return
    gaps = db.query(SkillGap).filter(SkillGap.job_match_id == job_match.id).all()
    for gap in gaps:
        _upsert(weaknesses, gap.skill_name, gap.gap_severity, "jd_gap")


def _from_assessment_results(db: Session, profile_id: int, jd_id: Optional[int], weaknesses: dict) -> None:
    query = (
        db.query(AssessmentResult, Assessment.id)
        .join(Assessment, Assessment.id == AssessmentResult.assessment_id)
        .join(AssessmentBlueprint, AssessmentBlueprint.id == Assessment.blueprint_id)
        .filter(Assessment.profile_id == profile_id)
    )
    query = query.filter(AssessmentBlueprint.jd_id == jd_id) if jd_id is not None else query.filter(
        AssessmentBlueprint.jd_id.is_(None)
    )

    for result, _assessment_id in query.all():
        for skill_tag in result.priority_weaknesses:
            score = result.skill_scores.get(skill_tag)
            if score is None:
                # The grader flagged this as a priority weakness but recorded no per-skill
                # score for it, so we know *that* it is weak and not *how* weak. This used to
                # substitute 0.5 and run it through the threshold below -- an invented number
                # dressed up as a measurement, and circular besides (the fake score existed
                # only to be compared). Classify it directly at the non-escalating rank
                # instead: it still surfaces as a weakness, but an unmeasured one can never
                # outrank a genuinely measured "high". If another source has real evidence,
                # _upsert promotes it on that evidence rather than on this default.
                severity = "medium"
            else:
                severity = "high" if score < _INTERVIEW_HIGH_SEVERITY_THRESHOLD else "medium"
            _upsert(weaknesses, skill_tag, severity, "test_result")


def _from_interview_evaluations(db: Session, profile_id: int, jd_id: Optional[int], weaknesses: dict) -> None:
    query = (
        db.query(InterviewEvaluation)
        .join(Interview, Interview.id == InterviewEvaluation.interview_id)
        .filter(Interview.profile_id == profile_id)
    )
    query = query.filter(Interview.jd_id == jd_id) if jd_id is not None else query.filter(Interview.jd_id.is_(None))

    for evaluation in query.all():
        for dimension in INTERVIEW_DIMENSIONS:
            score = getattr(evaluation, dimension)
            if score >= INTERVIEW_DIMENSION_THRESHOLD:
                continue
            severity = "high" if score < _INTERVIEW_HIGH_SEVERITY_THRESHOLD else "medium"
            _upsert(weaknesses, f"interview:{dimension}", severity, "interview_eval")


def compute_weaknesses(db: Session, profile_id: int, jd_id: Optional[int]) -> List[WeaknessItem]:
    """Deterministic (non-AI) aggregation across every signal the app already has about a
    student's weak spots -- JD skill gaps, test results, and interview evaluations. This is
    our own gap engine, matching the same "our own product logic, not delegated to AI"
    philosophy as skill_matching.skill_present. Only the roadmap *content* (task text) is
    AI-generated; *which* skills are weak is computed here from real, owned data.
    """
    weaknesses: dict = {}

    if jd_id is not None:
        _from_jd_skill_gaps(db, profile_id, jd_id, weaknesses)
    _from_assessment_results(db, profile_id, jd_id, weaknesses)
    _from_interview_evaluations(db, profile_id, jd_id, weaknesses)

    return sorted(weaknesses.values(), key=lambda w: (-_SEVERITY_RANK[w.severity], w.skill_tag))
