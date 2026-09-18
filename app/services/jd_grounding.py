"""Deterministic facts the JD preparation plan's AI step is allowed to write prose about.

Everything in this module is code, not AI. It decides WHICH gaps exist, WHICH resume keywords
are missing, and WHICH of the student's real projects line up with which JD requirement. The
AI that runs afterwards may only explain the things collected here -- jd_service drops any
generated entry keyed to something this module did not produce.

That split is the whole anti-fabrication mechanism, and it is deliberately code-level for the
same reason app/ai/input_guard.py is: a prompt instruction not to invent a project is only as
good as the model's compliance, whereas a project title that is not in this module's output
cannot survive validation no matter what the model returns.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.requirement import Requirement
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.services.skill_matching import skill_present

logger = logging.getLogger(__name__)

# Caps on what reaches the prompt. A JD can legitimately list dozens of requirements, and a
# resume can carry many projects; without limits the prompt grows unboundedly with attacker-
# influenced content and the AI call slows down for no added value to the student.
MAX_MISSING_KEYWORDS = 25
MAX_PROJECT_CONNECTIONS = 6
MAX_GAPS_WITH_GUIDANCE = 12

_MATCHABLE_CATEGORIES = ("skill", "tool")


@dataclass
class ResumeProject:
    """One project the student genuinely has, from their resume or their profile."""

    title: str
    description: Optional[str]
    tech_stack: Optional[str]
    source: str  # "resume" | "profile"

    def searchable_text(self) -> str:
        return " ".join(p for p in (self.title, self.tech_stack, self.description) if p)


@dataclass
class ResumeFacts:
    """What we actually know about the student's resume. Never invented, never defaulted to
    something plausible -- `has_resume` False means we genuinely have nothing to compare."""

    has_resume: bool
    skills: List[str] = field(default_factory=list)
    projects: List[ResumeProject] = field(default_factory=list)
    weak_sections: List[str] = field(default_factory=list)
    experience_count: int = 0
    project_count: int = 0
    certification_count: int = 0

    @property
    def is_informative(self) -> bool:
        """A resume we parsed but extracted almost nothing from cannot support tailored
        advice. Saying so is more useful than generating advice from an empty set."""
        return self.has_resume and bool(self.skills or self.projects)


@dataclass
class GroundedGap:
    """A gap established by the deterministic matching engine, carrying the JD's own framing
    of the requirement so the AI can never promote an inferred requirement to an explicit one."""

    skill: str
    severity: str
    is_explicit_requirement: bool
    requirement_importance: str
    requirement_value: str


@dataclass
class GroundedProjectMatch:
    requirement: str
    project: ResumeProject


def collect_resume_facts(db: Session, profile_id: int) -> ResumeFacts:
    """Read-only snapshot of the student's resume evidence.

    Deliberately does NOT raise when there is no resume. skill_matching.latest_resume_skills
    raises NoResumeAnalysisError, which Placement Path and Placement Alarm both rely on to
    block their flows -- this flow instead degrades to a JD-only plan, so it needs its own
    non-raising reader rather than a change to the shared one.
    """
    resume = (
        db.query(Resume)
        .filter(Resume.profile_id == profile_id, Resume.parse_status == "completed")
        .order_by(Resume.id.desc())
        .first()
    )
    analysis = (
        db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id == resume.id).first()
        if resume is not None
        else None
    )

    # Profile projects are first-class evidence the student entered themselves, and they exist
    # whether or not a resume was ever uploaded.
    profile_projects = [
        ResumeProject(title=p.title, description=p.description, tech_stack=p.tech_stack, source="profile")
        for p in db.query(Project).filter(Project.profile_id == profile_id).all()
        if p.title
    ]

    if analysis is None:
        return ResumeFacts(has_resume=False, projects=profile_projects)

    resume_projects: List[ResumeProject] = []
    for raw in analysis.extracted_projects or []:
        # Parsed JSON, so shapes are not guaranteed -- anything without a usable title is
        # skipped rather than rendered as an untitled project.
        if not isinstance(raw, dict):
            continue
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        resume_projects.append(
            ResumeProject(
                title=title.strip(),
                description=raw.get("description") if isinstance(raw.get("description"), str) else None,
                tech_stack=raw.get("tech_stack") if isinstance(raw.get("tech_stack"), str) else None,
                source="resume",
            )
        )

    return ResumeFacts(
        has_resume=True,
        skills=[s for s in (analysis.extracted_skills or []) if isinstance(s, str)],
        projects=resume_projects + profile_projects,
        weak_sections=[w for w in (analysis.weak_sections or []) if isinstance(w, str)],
        experience_count=len(analysis.extracted_experience or []),
        project_count=len(resume_projects),
        certification_count=len(analysis.extracted_certifications or []),
    )


def matchable_requirements(db: Session, jd_id: int) -> List[Requirement]:
    """The JD requirements that can meaningfully be matched against a resume -- skills and
    tools. Responsibilities, experience and education are requirements too, but they are prose
    rather than things a skill list can be checked against."""
    return (
        db.query(Requirement)
        .filter(Requirement.jd_id == jd_id, Requirement.category.in_(_MATCHABLE_CATEGORIES))
        .all()
    )


def ground_gaps(requirements: List[Requirement], gap_skills: dict) -> List[GroundedGap]:
    """Attach each gap to the Requirement row it came from.

    `gap_skills` maps skill name -> severity, as already determined by the matching engine and
    the weakness service. A gap whose skill matches no requirement row is dropped: without a
    requirement we cannot say how the JD framed it, and guessing is exactly what this module
    exists to prevent.
    """
    by_value = {r.value.lower(): r for r in requirements}
    grounded: List[GroundedGap] = []
    for skill, severity in gap_skills.items():
        requirement = by_value.get(skill.lower())
        if requirement is None:
            continue
        grounded.append(
            GroundedGap(
                skill=skill,
                severity=severity,
                is_explicit_requirement=bool(requirement.is_explicit),
                requirement_importance=requirement.importance,
                requirement_value=requirement.value,
            )
        )
    # Explicit, required gaps first: those are the ones a recruiter screens on.
    grounded.sort(key=lambda g: (not g.is_explicit_requirement, g.requirement_importance != "required"))
    return grounded[:MAX_GAPS_WITH_GUIDANCE]


def missing_keywords(requirements: List[Requirement], facts: ResumeFacts) -> List[str]:
    """JD terms that do not appear in the student's resume skills.

    Uses the same token-boundary check as the matching engine, so this cannot report a keyword
    as missing that the matcher considers present. Returns [] with no resume -- absence of a
    resume is not evidence that a keyword is missing from it.
    """
    if not facts.has_resume:
        return []
    seen: set = set()
    missing: List[str] = []
    for requirement in requirements:
        value = requirement.value.strip()
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        if not skill_present(value, facts.skills):
            missing.append(value)
    return missing[:MAX_MISSING_KEYWORDS]


def match_projects(requirements: List[Requirement], facts: ResumeFacts) -> List[GroundedProjectMatch]:
    """Pair JD requirements with the student's real projects that evidence them.

    A project qualifies only when the requirement's own words appear in what the student wrote
    about that project -- its title, stack or description. A skill listed on the resume is
    never enough on its own: "knows Azure" is not "built something with Azure", and presenting
    the second when only the first is true is the fabrication this whole feature must avoid.
    """
    matches: List[GroundedProjectMatch] = []
    claimed: set = set()
    for requirement in requirements:
        for project in facts.projects:
            key = (requirement.value.lower(), project.title.lower())
            if key in claimed:
                continue
            if skill_present(requirement.value, [project.searchable_text()]):
                matches.append(GroundedProjectMatch(requirement=requirement.value, project=project))
                claimed.add(key)
    return matches[:MAX_PROJECT_CONNECTIONS]


def render_resume_facts(facts: ResumeFacts) -> str:
    """The resume block as the prompt sees it. Summarised, never the raw resume text: the
    prompt needs enough to re-word honestly, and shipping an entire resume into every
    generation would put far more personal data through the provider than the task requires."""
    if not facts.has_resume:
        return "No resume uploaded. You have NO information about this student's experience."
    lines = [
        f"Skills listed: {', '.join(facts.skills) if facts.skills else 'none extracted'}",
        f"Work/internship entries: {facts.experience_count}",
        f"Projects on resume: {facts.project_count}",
        f"Certifications: {facts.certification_count}",
    ]
    if facts.weak_sections:
        lines.append(f"Sections our resume analysis already flagged as weak: {', '.join(facts.weak_sections)}")
    for project in facts.projects:
        detail = project.tech_stack or project.description or "no further detail given"
        lines.append(f"Project '{project.title}': {detail}")
    return "\n".join(lines)


def render_gap_context(gaps: List[GroundedGap]) -> str:
    if not gaps:
        return "none"
    lines = []
    for gap in gaps:
        framing = (
            "states this requirement outright"
            if gap.is_explicit_requirement
            else "does not name it; we inferred it"
        )
        lines.append(
            f"- {gap.skill}: the job description {framing}, "
            f"importance {gap.requirement_importance}, current status {gap.severity}"
        )
    return "\n".join(lines)


def render_project_context(matches: List[GroundedProjectMatch]) -> str:
    if not matches:
        return "none"
    return "\n".join(
        f"- project '{m.project.title}' matched to requirement '{m.requirement}'"
        f" (student's own description: {m.project.searchable_text()})"
        for m in matches
    )


# The exact phrase jd_preparation_plan_v4.txt instructs the model to use when the resume says
# nothing about an area. Compared case-insensitively and ignoring trailing punctuation, because
# the sentence is the contract, not its typography.
ABSENT_FACT_SENTINEL = "not present in your resume"

# Digits that carry no claim about the student. A recommendation may legitimately say
# "rewrite your top 3 bullet points" -- small ordinals are advice about the document, not an
# assertion that the student achieved something. Anything larger is treated as a metric and
# has to be traceable, which is what stops "reduced latency by 40%" being invented.
_UNCLAIMED_NUMBER_CEILING = 10

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _is_absent_fact(current_fact: str) -> bool:
    return current_fact.strip().rstrip(".").lower() == ABSENT_FACT_SENTINEL


def _fact_vocabulary(facts: ResumeFacts) -> List[str]:
    """Every phrase the deterministic layer can vouch for, as match targets.

    Project text is included whole (title, stack and description) because a recommendation
    that re-words a project the student really has is exactly what this feature is for.
    """
    vocabulary = list(facts.skills)
    vocabulary.extend(facts.weak_sections)
    for project in facts.projects:
        vocabulary.append(project.title)
        if project.tech_stack:
            vocabulary.append(project.tech_stack)
        if project.description:
            vocabulary.append(project.description)
    return [v for v in vocabulary if v and v.strip()]


def _numbers_the_resume_supports(facts: ResumeFacts) -> set:
    supported = {str(facts.experience_count), str(facts.project_count), str(facts.certification_count)}
    for phrase in _fact_vocabulary(facts):
        supported.update(_NUMBER_RE.findall(phrase))
    return supported


def ground_resume_recommendations(
    facts: ResumeFacts,
    gaps: List[GroundedGap],
    requirement_values: List[str],
    written_items: List,
) -> List:
    """Keep only the generated recommendations whose claim about the resume is traceable.

    The sibling sections (gap guidance, project connections) are safe by construction: they
    iterate a deterministic list and look the AI's prose up by key, so an invented entry is
    never looked up. Resume recommendations have no such key -- the model produces the list
    itself -- so the same guarantee has to be enforced by checking each item instead.

    Four rules, each closing a specific fabrication route observed in the schema's own
    wording ("current_fact is what the resume says today"):

      1. A contradicted gap is dropped. If the deterministic matcher established that the
         student is missing SQL, a current_fact asserting they already demonstrate SQL is
         false no matter how fluent it reads. This is the most dangerous case, because it is
         the one a student would repeat to a recruiter.

         This rule is deliberately blunt: it drops any mention of a gap skill, so an honest
         phrasing of the absence ("your resume does not mention SQL") is dropped too. That
         costs a usable recommendation and is the right way round -- the sanctioned way to say
         a thing is missing is the sentinel below, and a rule that tried to tell "does not
         mention SQL" from "demonstrates SQL" would be reading the model's prose for intent,
         which is the thing this module exists to stop relying on.
      2. A fact that mentions nothing we know is dropped. current_fact has to touch at least
         one real skill, project or flagged section -- otherwise it is a claim about a resume
         we cannot corroborate, including invented employers, which ResumeFacts deliberately
         does not carry names for.
      3. An untraceable number is dropped. Metrics are the classic resume fabrication, and
         any figure that is not one of our own counts, or already present in the student's own
         project text, is invented. Small ordinals are exempt -- see the ceiling above.
      4. An unknown requirement is dropped. `requirement` names a JD requirement, so a value
         the JD never stated is the model inventing what the employer asked for.

    The absence sentinel is exempt from rules 1-3: it asserts that the resume says nothing,
    which is the one claim that cannot overstate the student. Rule 4 still applies to it.

    Nothing is ever repaired or substituted -- an item that fails is dropped, because the only
    honest alternative to an unverifiable fact is no fact.
    """
    known_requirements = {value.strip().lower() for value in requirement_values if value and value.strip()}
    vocabulary = _fact_vocabulary(facts)
    supported_numbers = _numbers_the_resume_supports(facts)

    kept = []
    for item in written_items:
        current_fact = (item.current_fact or "").strip()
        if not current_fact:
            logger.info("Dropped a resume recommendation with an empty current_fact")
            continue

        requirement = (item.requirement or "").strip()
        if requirement and requirement.lower() not in known_requirements:
            logger.info("Dropped a resume recommendation citing a requirement this JD never stated")
            continue

        if _is_absent_fact(current_fact):
            kept.append(item)
            continue

        contradicted = next((gap for gap in gaps if skill_present(gap.skill, [current_fact])), None)
        if contradicted is not None:
            logger.info("Dropped a resume recommendation claiming a skill the matcher recorded as a gap")
            continue

        if not any(skill_present(phrase, [current_fact]) for phrase in vocabulary):
            logger.info("Dropped a resume recommendation whose current_fact matched no known resume fact")
            continue

        invented_number = next(
            (
                number
                for number in _NUMBER_RE.findall(current_fact)
                if number not in supported_numbers and float(number) > _UNCLAIMED_NUMBER_CEILING
            ),
            None,
        )
        if invented_number is not None:
            logger.info("Dropped a resume recommendation carrying a metric the resume does not support")
            continue

        kept.append(item)

    return kept
