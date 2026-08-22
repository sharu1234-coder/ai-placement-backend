import re

from sqlalchemy.orm import Session

from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


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


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT_RE.split(text.lower()) if t}


def skill_present(skill_name: str, resume_skills: list[str]) -> bool:
    """Token-boundary match, not raw substring: "R" must not match inside "Docker", and an
    empty skill_name must never match anything. A previous plain `in` substring check let
    both slip through -- e.g. "r" in "docker" is True -- silently fabricating skill matches
    the resume doesn't actually contain, in violation of "never invent resume facts" (see
    PROJECT_HANDOFF.md section 5, extended here from AI output to this deterministic check).
    "React" still matches "react.js" (its tokens are a subset of the resume entry's), and
    vice versa -- same cases the existing tests already covered."""
    skill_tokens = _tokens(skill_name)
    if not skill_tokens:
        return False
    for resume_skill in resume_skills:
        resume_tokens = _tokens(resume_skill)
        if resume_tokens and (skill_tokens <= resume_tokens or resume_tokens <= skill_tokens):
            return True
    return False
