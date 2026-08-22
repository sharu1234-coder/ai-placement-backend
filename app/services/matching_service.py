from typing import Optional

from sqlalchemy.orm import Session

from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.requirement import Requirement
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.skill_gap import SkillGap
from app.schemas.job import JobMatchRead, SkillGapRead

_REQUIRED_POINTS = 2
_PREFERRED_POINTS = 1


class NoResumeAnalysisError(Exception):
    """Raised when the profile has no completed resume analysis to match against."""


def _latest_resume_skills(db: Session, profile_id: int) -> list[str]:
    resume = (
        db.query(Resume)
        .filter(Resume.profile_id == profile_id, Resume.parse_status == "completed")
        .order_by(Resume.id.desc())
        .first()
    )
    if resume is None:
        raise NoResumeAnalysisError("No completed resume analysis found for this profile")

    analysis = db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume.id).first()
    if analysis is None:
        raise NoResumeAnalysisError("No completed resume analysis found for this profile")

    return [s.lower() for s in analysis.extracted_skills]


def _skill_present(skill_name: str, resume_skills: list[str]) -> bool:
    skill_lower = skill_name.lower()
    return any(skill_lower in rs or rs in skill_lower for rs in resume_skills)


def match_resume_to_job(db: Session, jd_id: int, profile_id: int) -> Optional[JobMatchRead]:
    """Deterministic (non-AI) skill matching -- explainable, fast, and testable. This is
    our own product logic per GITHUB_REFERENCES.md section 6, not delegated to the AI
    provider or any third-party matching algorithm."""
    jd = db.get(JobDescription, jd_id)
    if jd is None or jd.profile_id != profile_id:
        return None

    resume_skills = _latest_resume_skills(db, profile_id)

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
        if _skill_present(req.value, resume_skills):
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
