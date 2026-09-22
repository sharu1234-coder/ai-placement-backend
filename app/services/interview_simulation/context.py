"""The bounded bundle of real student facts a simulation question may be grounded in.

Read-only, in-memory, never persisted. Every field is read from the service that already owns
it -- role_context, jd_grounding, weakness_record_service, and the provider-evidence rows the
Evidence Engine reads -- so this module computes nothing of its own and cannot become a second
source of truth for anything.

Two rules govern what is allowed in here, and both are enforced by construction rather than by
asking the model nicely:

1. Nothing the student has not actually said about themselves may be presented as a claim.
   JD requirements are topics to ask *about*; only `resume_claims` and `projects` -- read from
   the student's own parsed resume and their own project rows -- describe what they have done.
   A recruiter may say "this role needs Kafka, what's your exposure?"; it must never say "you
   worked with Kafka at...". Keeping those in separate fields is what makes that a structural
   guarantee instead of a prompt instruction.

2. Everything is capped. A JD can list dozens of requirements and a resume can list many
   skills; without limits the prompt grows without bound with attacker-influenced content, the
   local model's context window fills up somewhere around the closing stages, and every
   generation gets slower for no added value. The caps below are deliberately small -- the same
   reasoning, and roughly the same sizes, as jd_grounding's own MAX_* constants.

Deliberately absent, and to stay absent: user ids, emails, profile ids, interview or session
ids, tokens of any kind, provider credentials, and anything belonging to another student.
Every query here is scoped to the one profile passed in.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.external_evidence_item import SIGNAL_REPO_TOPIC, ExternalEvidenceItem
from app.models.interview import Interview
from app.models.job_description import JobDescription
from app.models.profile import StudentProfile
from app.models.provider_connection import ProviderConnection
from app.services import jd_grounding
from app.services.role_context import resolve_role_context
from app.services.weakness_record_service import list_for_profile

# Caps on everything that reaches a prompt. See the module docstring for why these are small.
MAX_JD_REQUIREMENTS = 12
MAX_RESUME_CLAIMS = 10
MAX_PROJECTS = 3
MAX_PRIORITY_SKILLS = 8
MAX_REPEATED_WEAKNESSES = 3
MAX_GITHUB_TOPICS = 10
MAX_PROJECT_CHARS = 400
MAX_CLAIM_CHARS = 160

# How much of the conversation so far the recruiter is reminded of. Fixed and small on purpose:
# an 18-turn transcript replayed in full would grow the prompt monotonically and crowd out the
# grounding facts by the closing stages. Six pairs is enough to avoid repeating a question and
# to follow up on something just said, which is all Phase 2B's adaptation needs.
MAX_PRIOR_TURNS = 6

# ...and how much of each one. Six pairs alone is not a bound on size: an answer may be up to
# 5,000 characters (SubmitInterviewAnswerRequest's own limit), so six unabridged pairs measured
# at ~32KB -- five times the entire grounding bundle, and enough to crowd a local model's
# context window by the closing stages. A follow-up needs the gist of what was just said, not a
# verbatim replay, so each side of each turn is trimmed. Trimmed, never dropped: the most recent
# exchange still appears, just shorter.
MAX_TURN_CHARS = 500

# A weakness the student has hit more than once. Two or more occurrences is the existing
# "repeated" bar weakness_record_service's own status machine uses; nothing new is computed.
_REPEATED_OCCURRENCE_THRESHOLD = 2


@dataclass
class SimulationContext:
    """What the recruiter knows. Every list is already capped and already trimmed."""

    # Trusted -- our own role catalog and our own computations, never student- or JD-supplied
    # free text.
    role_name: str
    priority_skills: List[str] = field(default_factory=list)
    repeated_weaknesses: List[str] = field(default_factory=list)

    # Untrusted -- written by the student, or by whoever wrote the job posting. Every one of
    # these is interpolated into a delimited block through render_prompt.
    jd_title: Optional[str] = None
    jd_requirements: List[str] = field(default_factory=list)
    resume_claims: List[str] = field(default_factory=list)
    projects: List[str] = field(default_factory=list)
    github_topics: List[str] = field(default_factory=list)

    @property
    def has_jd(self) -> bool:
        return self.jd_title is not None or bool(self.jd_requirements)


def _trim(value: str, limit: int) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _jd_facts(db: Session, profile_id: int, jd_id: Optional[int]) -> tuple[Optional[str], List[str]]:
    """Title and requirement values for an owned JD, or nothing.

    Ownership is re-checked here even though interview_service validated jd_id when the
    interview was created, because a JD can be deleted or reassigned between those two moments
    and this module must never read a row it cannot prove belongs to this student. Parsed
    `Requirement` rows are used rather than the JD's raw text: they are already structured,
    already bounded, and reusing jd_grounding's reader keeps JD parsing in exactly one place.
    """
    if jd_id is None:
        return None, []
    jd = db.get(JobDescription, jd_id)
    if jd is None or jd.profile_id != profile_id:
        return None, []

    requirements = jd_grounding.matchable_requirements(db, jd_id)
    values = [_trim(r.value, MAX_CLAIM_CHARS) for r in requirements if r.value and r.value.strip()]
    return jd.role_title, values[:MAX_JD_REQUIREMENTS]


def _github_topics(db: Session, profile_id: int) -> List[str]:
    """Topics on repositories the student connected, if any.

    Topics are the weakest external signal there is -- a label the student typed onto their own
    repository -- so they are used for one thing only: helping choose what to ask about. They
    are never presented as evidence that the student can explain the thing, and they are read
    through a join on a live ProviderConnection so a revoked connection stops contributing
    immediately.
    """
    rows = (
        db.query(ExternalEvidenceItem.skill)
        .join(ProviderConnection, ProviderConnection.id == ExternalEvidenceItem.connection_id)
        .filter(
            ProviderConnection.profile_id == profile_id,
            ProviderConnection.revoked_at.is_(None),
            ExternalEvidenceItem.signal_kind == SIGNAL_REPO_TOPIC,
        )
        .order_by(ExternalEvidenceItem.observed_at.desc())
        .limit(MAX_GITHUB_TOPICS)
        .all()
    )
    seen: List[str] = []
    for (skill,) in rows:
        if skill and skill not in seen:
            seen.append(skill)
    return seen


def build(db: Session, profile: StudentProfile, interview: Interview) -> SimulationContext:
    """Assemble the context for one simulation.

    A fixed, small number of queries regardless of how far into the interview we are -- nothing
    here is per-question, and nothing here loops over questions. It is rebuilt per next-question
    request because the request is the only scope that exists (there is no cross-request cache
    in this app, and adding one would be new infrastructure rather than Phase 2B); what matters
    for cost is that the assembly is bounded and N+1-free, which it is.
    """
    role = resolve_role_context(db, profile)
    jd_title, jd_requirements = _jd_facts(db, profile.id, interview.jd_id)

    # The role name the recruiter uses. A JD's own title wins when there is one, because that is
    # the job actually being interviewed for; otherwise the student's resolved target role. Never
    # fabricated -- with neither, the prompt says so rather than inventing a role.
    role_name = (jd_title or (role.display_role_name if role is not None else None) or "the target role")

    facts = jd_grounding.collect_resume_facts(db, profile.id)
    resume_claims = [_trim(s, MAX_CLAIM_CHARS) for s in facts.skills if s and s.strip()][:MAX_RESUME_CLAIMS]
    projects = [
        _trim(
            f"{p.title} — {p.tech_stack or 'stack not stated'}: {p.description or 'no description given'}",
            MAX_PROJECT_CHARS,
        )
        for p in facts.projects
    ][:MAX_PROJECTS]

    # Highest-weighted skills for the role, from the existing skill graph. Used to pick the
    # technical subject matter; it says nothing about whether the student has the skill.
    priority_skills = (
        [s for s, _ in sorted(role.weights.items(), key=lambda kv: (-kv[1], kv[0]))][:MAX_PRIORITY_SKILLS]
        if role is not None
        else []
    )

    repeated = [
        record.skill_key.removeprefix("interview:").removeprefix("recruiter_attack:").replace("_", " ")
        for record in list_for_profile(db, profile.id)
        if record.occurrences >= _REPEATED_OCCURRENCE_THRESHOLD and record.status != "CLOSED"
    ][:MAX_REPEATED_WEAKNESSES]

    return SimulationContext(
        role_name=role_name,
        priority_skills=priority_skills,
        repeated_weaknesses=repeated,
        jd_title=jd_title,
        jd_requirements=jd_requirements,
        resume_claims=resume_claims,
        projects=projects,
        github_topics=_github_topics(db, profile.id),
    )


def _bullets(values: List[str], empty: str) -> str:
    return "\n".join(f"- {v}" for v in values) if values else empty


def render_jd_block(context: SimulationContext) -> str:
    if not context.has_jd:
        return "No job description was provided for this interview."
    title = context.jd_title or "(no title given)"
    return f"Role title: {title}\nWhat the posting asks for:\n" + _bullets(
        context.jd_requirements, "- (no specific requirements were parsed)"
    )


def render_claims_block(context: SimulationContext) -> str:
    return _bullets(
        context.resume_claims,
        "- (the candidate has no analysed resume on file -- do not attribute any experience to them)",
    )


def render_projects_block(context: SimulationContext) -> str:
    return _bullets(
        context.projects,
        "- (the candidate has not listed any projects -- ask what they have built rather than naming one)",
    )


def render_focus_block(context: SimulationContext) -> str:
    """The trusted half: what this role needs, and what this student has repeatedly struggled
    with. Kept in one block separate from everything the student or a job poster wrote."""
    lines: List[str] = []
    if context.priority_skills:
        lines.append("Skills this role weighs most heavily: " + ", ".join(context.priority_skills))
    if context.github_topics:
        lines.append(
            "Topics on repositories they have connected (a hint for what to ask about, NOT proof "
            "they can explain it): " + ", ".join(context.github_topics)
        )
    if context.repeated_weaknesses:
        lines.append(
            "Areas they have been marked down on in more than one past interview: "
            + ", ".join(context.repeated_weaknesses)
        )
    return "\n".join(lines) if lines else "(no additional focus areas on file)"


def render_recent_turns(pairs: List[tuple], limit: int = MAX_PRIOR_TURNS) -> str:
    """The last few question/answer pairs, oldest first. `pairs` is [(question, answer|None)]."""
    recent = pairs[-limit:]
    if not recent:
        return "Nothing yet -- this is the opening question of the interview."
    return "\n".join(
        f"Q: {_trim(question, MAX_TURN_CHARS)}\n"
        f"A: {_trim(answer, MAX_TURN_CHARS) if answer else '(no answer given)'}"
        for question, answer in recent
    )
