from typing import Optional

from sqlalchemy.orm import Session

from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.requirement import Requirement
from app.models.skill_gap import SkillGap
from app.schemas.job import JobMatchRead, SkillGapRead
from app.services.skill_matching import latest_resume_skills, skill_present

_REQUIRED_POINTS = 2
_PREFERRED_POINTS = 1


def read_match(db: Session, profile_id: int, match_id: int) -> Optional[JobMatchRead]:
    """A stored match as match_resume_to_job would have returned it, or None.

    Answers a replayed match from the row the first attempt scored. Matching is deterministic,
    so re-running it would produce the same numbers -- but it would also insert a second
    JobMatch and a second set of SkillGap rows, which is what this prevents.
    """
    match = db.get(JobMatch, match_id)
    if match is None or match.profile_id != profile_id:
        return None
    gaps = db.query(SkillGap).filter(SkillGap.jd_id == match.jd_id).all()
    return JobMatchRead(
        id=match.id,
        matching_skills=match.matching_skills,
        missing_skills=match.missing_skills,
        match_score=match.match_score,
        skill_gaps=[
            SkillGapRead(id=g.id, skill_name=g.skill_name, gap_severity=g.gap_severity,
                         source=g.source)
            for g in gaps
        ],
    )


def match_resume_to_job(db: Session, jd_id: int, profile_id: int) -> Optional[JobMatchRead]:
    """Deterministic (non-AI) skill matching -- explainable, fast, and testable. This is
    our own product logic per GITHUB_REFERENCES.md section 6, not delegated to the AI
    provider or any third-party matching algorithm."""
    jd = db.get(JobDescription, jd_id)
    if jd is None or jd.profile_id != profile_id:
        return None

    resume_skills = latest_resume_skills(db, profile_id)

    jd_skill_requirements = (
        db.query(Requirement)
        .filter(Requirement.jd_id == jd_id, Requirement.category == "skill")
        .all()
    )

    matching: list[str] = []
    missing_gaps: list[tuple[str, str]] = []  # (skill_name, severity)
    earned_points = 0
    max_points = 0

    for req in jd_skill_requirements:
        points = _REQUIRED_POINTS if req.importance == "required" else _PREFERRED_POINTS
        max_points += points
        if skill_present(req.value, resume_skills):
            matching.append(req.value)
            earned_points += points
        else:
            severity = "high" if req.importance == "required" else "medium"
            missing_gaps.append((req.value, severity))

    match_score = round((earned_points / max_points) * 100, 1) if max_points > 0 else 0.0

    job_match = JobMatch(
        jd_id=jd_id,
        profile_id=profile_id,
        matching_skills=matching,
        missing_skills=[name for name, _ in missing_gaps],
        match_score=match_score,
    )
    db.add(job_match)
    db.commit()
    db.refresh(job_match)

    skill_gaps = [
        SkillGap(job_match_id=job_match.id, skill_name=name, gap_severity=severity, source="jd_match")
        for name, severity in missing_gaps
    ]
    db.add_all(skill_gaps)
    db.commit()
    for gap in skill_gaps:
        db.refresh(gap)

    return JobMatchRead(
        id=job_match.id,
        matching_skills=job_match.matching_skills,
        missing_skills=job_match.missing_skills,
        match_score=job_match.match_score,
        skill_gaps=[
            SkillGapRead(id=g.id, skill_name=g.skill_name, gap_severity=g.gap_severity, source=g.source)
            for g in skill_gaps
        ],
    )
