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


# An allowlist, not a denylist: several features (the Placement Alarm, the Placement Path
# roadmap) must ONLY draw on resume skills that are themselves recognizable placement/
# interview-prep technical or aptitude topics. A resume skill not covered here (soft skills,
# hobbies, anything else) is excluded even though it's a real resume entry -- "on the resume"
# is necessary but not sufficient. This is a maintainable curated set shared by every feature
# that needs this filter, not an exhaustive taxonomy; it never grows to include a topic just to
# make one more resume skill match -- new entries must genuinely be placement/interview-prep
# topics. Originally lived in placement_alarm_service.py; promoted here so every feature reads
# from one true allowlist instead of maintaining separate copies that could drift apart.
PLACEMENT_TOPIC_KEYWORDS = frozenset(
    {
        # Programming languages
        "python", "java", "c", "c++", "c#", "javascript", "typescript", "kotlin", "swift",
        "go", "golang", "rust", "r", "scala", "php", "ruby", "dart", "matlab",
        # CS fundamentals
        "dsa", "data structures", "data structures and algorithms", "algorithms",
        "oop", "oops", "object oriented programming", "dbms",
        "database management system", "operating system", "operating systems",
        "computer networks", "computer network", "system design", "compiler design",
        "theory of computation", "automata",
        # Aptitude / reasoning -- core placement-test topics
        "aptitude", "quantitative aptitude", "logical reasoning", "verbal ability",
        "reasoning", "numerical ability",
        # AI / ML / data
        "machine learning", "ml", "deep learning", "artificial intelligence", "ai",
        "data science", "natural language processing", "nlp", "computer vision",
        "neural networks", "statistics", "data analysis", "data analytics",
        "pandas", "numpy", "tensorflow", "pytorch", "scikit learn", "big data",
        # Databases
        "sql", "mysql", "postgresql", "postgres", "mongodb", "nosql", "oracle", "sqlite",
        # Web / backend
        "html", "css", "react", "angular", "vue", "node", "nodejs", "django", "flask",
        "fastapi", "spring", "spring boot", "rest api", "graphql", "microservices",
        "web development",
        # Cloud / devops
        "aws", "azure", "gcp", "cloud computing", "docker", "kubernetes", "devops",
        "ci cd", "git", "github", "linux", "shell scripting",
        # Security / testing
        "cybersecurity", "network security", "testing", "unit testing",
        "software testing", "automation testing",
        # Mobile / general software
        "android", "ios", "flutter", "react native", "software engineering",
        "software development", "version control", "agile",
    }
)


def is_placement_topic(skill: str) -> bool:
    """Token-boundary match against PLACEMENT_TOPIC_KEYWORDS, mirroring skill_present's
    reasoning: raw substring checks would let "r" match inside "docker" or "css" match inside
    unrelated text. A resume skill counts as a placement topic only if its tokens are a subset
    of an allowlist keyword's tokens, or vice versa -- so "Data Structures & Algorithms (DSA)"
    matches "dsa", and "React.js" matches "react"."""
    skill_tokens = _tokens(skill)
    if not skill_tokens:
        return False
    for keyword in PLACEMENT_TOPIC_KEYWORDS:
        keyword_tokens = _tokens(keyword)
        if keyword_tokens and (keyword_tokens <= skill_tokens or skill_tokens <= keyword_tokens):
            return True
    return False
