from sqlalchemy.orm import Session

from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis


class NoResumeAnalysisError(Exception):
    """Raised when the profile has no completed resume analysis to match against."""


def latest_resume_skills(db: Session, profile_id: int) -> list[str]:
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


def skill_present(skill_name: str, resume_skills: list[str]) -> bool:
    skill_lower = skill_name.lower()
    return any(skill_lower in rs or rs in skill_lower for rs in resume_skills)
