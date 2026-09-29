"""Turn a stored provider snapshot into observations a student can act on.

The division of labour here is the same one the preparation plan already uses, for the same
reason: every observation is established in Python from stored facts, and the model is only ever
asked to write prose for an observation that already exists. The assembly loop iterates the
deterministic list and looks the model's text up by code, so an observation the model invented
has no entry to attach to and never reaches the student, while one it ignored still appears with
a hand-written sentence.

Each observation carries the `evidence` sentence that produced it, stating counts and names from
the snapshot. That is a product requirement rather than a nicety: a recommendation a student
cannot trace back to something real is indistinguishable from flattery, and this feature exists
precisely because generic profile advice is worthless.

What is deliberately NOT observed:

  * Nothing about code quality. A repository is not a graded assessment and we have not read the
    code -- only the languages GitHub measured and the labels the owner typed.
  * Nothing about employment. Owning a repository under an organisation says nothing about who
    employs whom, and inferring it would be a fabricated fact about the student's history.
  * Nothing about commit frequency or streaks. That data lives behind the `read:user` scope,
    which this app deliberately does not request, so the honest position is silence rather than
    an estimate dressed up as a measurement.
  * No hiring or shortlisting probability, ever. There is no such field anywhere in this module,
    which is a stronger guarantee than remembering not to populate one.
"""

import datetime
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.ai.input_guard import render_prompt
from app.ai.output_guard import looks_like_prompt_leak
from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.professional_presence import GeneratedPresenceNarrative
from app.models.external_profile_snapshot import ExternalProfileSnapshot
from app.models.skill import Skill
from app.services.skill_matching import NoResumeAnalysisError, latest_resume_skills, skill_present

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "ai" / "prompts" / "professional_presence_audit_v1.txt"
# Stored beside the generated text so a historical snapshot records which prompt produced it.
# Without it, a change to the prompt silently reinterprets every audit already written.
PROMPT_VERSION = "professional_presence_audit_v1"

# Observation codes. A closed vocabulary, because it is the join key between the deterministic
# half and the generated half -- if the model could introduce a code, it could introduce an
# observation, which is the whole thing this design prevents.
CODE_NO_BIO = "profile_no_bio"
CODE_NO_DISPLAY_NAME = "profile_no_display_name"
CODE_NO_PUBLIC_REPOS = "no_public_repositories"
CODE_REPOS_WITHOUT_DESCRIPTION = "repositories_without_description"
CODE_REPOS_WITHOUT_TOPICS = "repositories_without_topics"
CODE_MOSTLY_FORKS = "mostly_forks"
CODE_WORK_IS_STALE = "work_is_stale"
CODE_SKILL_WITHOUT_EVIDENCE = "declared_skill_without_evidence"
CODE_LANGUAGE_STRENGTH = "language_strength"

OBSERVATION_CODES = (
    CODE_NO_BIO,
    CODE_NO_DISPLAY_NAME,
    CODE_NO_PUBLIC_REPOS,
    CODE_REPOS_WITHOUT_DESCRIPTION,
    CODE_REPOS_WITHOUT_TOPICS,
    CODE_MOSTLY_FORKS,
    CODE_WORK_IS_STALE,
    CODE_SKILL_WITHOUT_EVIDENCE,
    CODE_LANGUAGE_STRENGTH,
)

KIND_STRENGTH = "strength"
KIND_GAP = "gap"
KIND_OPPORTUNITY = "opportunity"

# Work older than this is described as not recent. Matches the Evidence Engine's staleness
# window so the two features cannot tell a student different things about the same repository.
_STALE_DAYS = 90

# Below this many evidence-bearing repositories, "most of your repositories are forks" is not a
# meaningful observation -- one fork out of one repository is noise, not a pattern.
_MIN_REPOS_FOR_RATIO = 3
_FORK_RATIO_THRESHOLD = 0.6

# At most this many "you claim X but nothing shows it" observations. Past a handful the screen
# stops being a set of actions and becomes a list of accusations, and the student cannot act on
# twenty at once anyway.
_MAX_SKILL_GAPS = 5
_MAX_STRENGTHS = 3


@dataclass(frozen=True)
class AuditObservation:
    """One thing that is true about this student's public presence, and how we know.

    `evidence` is the part that must never be generated: it is built from counts and names taken
    straight out of the snapshot, so the claim and its proof cannot drift apart.
    """

    code: str
    kind: str
    subject: Optional[str]
    evidence: str
    # The deterministic sentence shown when no generated prose exists for this code -- because
    # the model failed, returned nothing for it, or returned an entry we rejected.
    fallback_action: str


def _repos(payload: Dict) -> List[Dict]:
    raw = payload.get("repositories")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _own_repos(payload: Dict) -> List[Dict]:
    """Repositories the student actually wrote: not forks, not archived.

    Forks are excluded from every presentation observation because telling someone to write a
    description for somebody else's project is bad advice, and counting it against them is worse.
    """
    return [r for r in _repos(payload) if not r.get("is_fork") and not r.get("is_archived")]


def _parse_dt(value) -> Optional[datetime.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _profile_observations(payload: Dict) -> List[AuditObservation]:
    profile = payload.get("profile") or {}
    observations: List[AuditObservation] = []

    if not profile.get("name"):
        observations.append(
            AuditObservation(
                code=CODE_NO_DISPLAY_NAME,
                kind=KIND_OPPORTUNITY,
                subject=None,
                evidence="Your GitHub profile has no display name set.",
                fallback_action="Add your full name to your GitHub profile so a recruiter can match it to your resume.",
            )
        )
    if not profile.get("bio"):
        observations.append(
            AuditObservation(
                code=CODE_NO_BIO,
                kind=KIND_OPPORTUNITY,
                subject=None,
                evidence="Your GitHub profile has no bio.",
                fallback_action="Write a one-line bio naming the kind of work you want to be hired for.",
            )
        )
    return observations


def _repository_observations(payload: Dict) -> List[AuditObservation]:
    own = _own_repos(payload)
    observations: List[AuditObservation] = []

    if not own:
        observations.append(
            AuditObservation(
                code=CODE_NO_PUBLIC_REPOS,
                kind=KIND_GAP,
                subject=None,
                evidence="You have no public repositories of your own that we can see.",
                fallback_action="Publish one project you have already built. One real repository outweighs an empty profile.",
            )
        )
        return observations

    missing_description = [r for r in own if not r.get("description")]
    if missing_description:
        names = ", ".join(sorted(r.get("full_name", "") for r in missing_description)[:3])
        observations.append(
            AuditObservation(
                code=CODE_REPOS_WITHOUT_DESCRIPTION,
                kind=KIND_OPPORTUNITY,
                subject=None,
                evidence=(
                    f"{len(missing_description)} of your {len(own)} public repositories have no "
                    f"description, including {names}."
                ),
                fallback_action="Add a one-sentence description to each repository saying what it does and for whom.",
            )
        )

    missing_topics = [r for r in own if not r.get("topics")]
    if missing_topics:
        observations.append(
            AuditObservation(
                code=CODE_REPOS_WITHOUT_TOPICS,
                kind=KIND_OPPORTUNITY,
                subject=None,
                evidence=(
                    f"{len(missing_topics)} of your {len(own)} public repositories have no topics set."
                ),
                fallback_action="Add topics naming the technologies each project uses, so your work is findable.",
            )
        )

    all_repos = _repos(payload)
    forks = [r for r in all_repos if r.get("is_fork")]
    if len(all_repos) >= _MIN_REPOS_FOR_RATIO and len(forks) / len(all_repos) >= _FORK_RATIO_THRESHOLD:
        observations.append(
            AuditObservation(
                code=CODE_MOSTLY_FORKS,
                kind=KIND_GAP,
                subject=None,
                evidence=f"{len(forks)} of your {len(all_repos)} public repositories are forks of other people's projects.",
                fallback_action="Add original work. A fork shows interest; it does not show what you can build.",
            )
        )

    pushed = [dt for dt in (_parse_dt(r.get("pushed_at")) for r in own) if dt is not None]
    if pushed:
        newest = max(pushed)
        age_days = (datetime.datetime.now(datetime.timezone.utc) - newest).days
        if age_days > _STALE_DAYS:
            observations.append(
                AuditObservation(
                    code=CODE_WORK_IS_STALE,
                    kind=KIND_GAP,
                    subject=None,
                    evidence=f"Your most recent public commit was {age_days} days ago.",
                    fallback_action="Push something small. A profile that has been still for months reads as inactive.",
                )
            )

    return observations


def _strength_observations(payload: Dict) -> List[AuditObservation]:
    """Languages the student demonstrably writes, ranked by how many repositories use them.

    Counted from GitHub's own measurement of the code, never from topics -- a strength claimed
    by a label the student typed is not a strength, and presenting it as one would be the exact
    failure this feature is meant to correct.
    """
    counts: Dict[str, int] = {}
    for repo in _own_repos(payload):
        for language in repo.get("languages") or []:
            if isinstance(language, str) and language:
                counts[language] = counts.get(language, 0) + 1

    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    observations = []
    for language, count in ranked[:_MAX_STRENGTHS]:
        observations.append(
            AuditObservation(
                code=CODE_LANGUAGE_STRENGTH,
                kind=KIND_STRENGTH,
                subject=language,
                evidence=(
                    f"GitHub measures {language} in {count} of your public "
                    f"{'repository' if count == 1 else 'repositories'}."
                ),
                fallback_action=f"Lead with {language} on your resume -- you have public code to back it up.",
            )
        )
    return observations


def _claim_gap_observations(db: Session, profile_id: int, payload: Dict) -> List[AuditObservation]:
    """Skills the student says they have, with nothing on GitHub showing them.

    Framed as a gap in *evidence*, never as a doubt about the student. The distinction is not
    cosmetic: plenty of real skills leave no public trace, and a tool that calls those a lie is
    both wrong and useless. The honest statement is "your public work does not show this yet",
    and the action is to make it show.

    Only computed when there is something to compare against. With no repositories at all, every
    declared skill would trivially produce a gap and the screen would become a wall of noise
    that says nothing the "no public repositories" observation has not already said.
    """
    own = _own_repos(payload)
    if not own:
        return []

    observed: List[str] = []
    for repo in own:
        for language in repo.get("languages") or []:
            if isinstance(language, str):
                observed.append(language)
        for topic in repo.get("topics") or []:
            if isinstance(topic, str):
                observed.append(topic)
    if not observed:
        return []

    declared = [row.name for row in db.query(Skill).filter(Skill.profile_id == profile_id).all()]
    try:
        declared += latest_resume_skills(db, profile_id)
    except NoResumeAnalysisError:
        pass

    seen: set = set()
    observations: List[AuditObservation] = []
    for skill in declared:
        key = skill.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if skill_present(skill, observed):
            continue
        observations.append(
            AuditObservation(
                code=CODE_SKILL_WITHOUT_EVIDENCE,
                kind=KIND_GAP,
                subject=skill,
                evidence=(
                    f"You list {skill} as a skill, and none of your {len(own)} public "
                    f"{'repository' if len(own) == 1 else 'repositories'} shows it."
                ),
                fallback_action=f"Publish something small that uses {skill}, so the claim has public backing.",
            )
        )
        if len(observations) >= _MAX_SKILL_GAPS:
            break
    return observations


def observations_for(db: Session, profile_id: int, snapshot: ExternalProfileSnapshot) -> List[AuditObservation]:
    """Every observation, computed fresh from the stored snapshot and the student's own records.

    Computed at read time rather than stored, because half of these depend on facts that change
    without GitHub changing: a student can add a skill or upload a new resume at any moment, and
    a stored audit would go quietly wrong the instant they did.
    """
    payload = snapshot.payload if isinstance(snapshot.payload, dict) else {}
    return [
        *_strength_observations(payload),
        *_repository_observations(payload),
        *_profile_observations(payload),
        *_claim_gap_observations(db, profile_id, payload),
    ]


def _narrative_key(code: str, subject: Optional[str]) -> str:
    """The join key between an observation and its generated prose.

    Subject is folded to lower case because the model echoes back a name it was shown, and
    "Python" versus "python" is not a difference worth losing a usable sentence over. The code
    is matched exactly, because that half is a closed vocabulary the model was given verbatim.
    """
    return f"{code}|{(subject or '').strip().lower()}"


def _render_observations(observations: List[AuditObservation]) -> str:
    lines = []
    for obs in observations:
        subject = f" subject={obs.subject}" if obs.subject else ""
        lines.append(f"- code={obs.code}{subject} evidence={obs.evidence}")
    return "\n".join(lines)


def generate_narrative(
    observations: List[AuditObservation], ai_provider: AIProvider
) -> Dict[str, Dict[str, str]]:
    """Ask for prose, then keep only what corresponds to a real observation.

    Returns a mapping keyed by observation, so assembly is a lookup rather than a merge. An
    entry whose code or subject the deterministic layer did not produce is dropped rather than
    repaired: there is no honest way to attach a sentence to an observation that does not exist,
    and repairing it would mean deciding what the model meant.

    Never raises. A failed or malformed generation yields an empty mapping and every observation
    falls back to its own hand-written action -- the audit is smaller, not wrong.
    """
    if not observations:
        return {}

    allowed = {_narrative_key(obs.code, obs.subject) for obs in observations}

    try:
        template = _PROMPT_PATH.read_text(encoding="utf-8")
        prompt = render_prompt(template, observations=_render_observations(observations))
        generated = ai_provider.generate_structured(prompt, GeneratedPresenceNarrative)
    except AIGenerationError as exc:
        logger.warning("event=presence_narrative_failed reason=%s", type(exc).__name__)
        return {}
    except OSError:
        logger.warning("event=presence_narrative_failed reason=prompt_unreadable")
        return {}

    narrative: Dict[str, Dict[str, str]] = {}
    for item in generated.items:
        key = _narrative_key(item.code, item.subject)
        if key not in allowed or key in narrative:
            # Unknown observation, or a second entry for one already written. Both are the
            # model going beyond what it was asked, and both are simply not used.
            continue
        why = item.why_it_matters.strip()
        action = item.recommended_action.strip()
        if not why or not action:
            continue
        if looks_like_prompt_leak(why) or looks_like_prompt_leak(action):
            # Dropped exactly like an unknown observation above: the finding still reaches the
            # student, carrying its own hand-written action and is_template=True. An audit that
            # is smaller is the documented outcome here; one that is wrong is not.
            continue
        narrative[key] = {"why_it_matters": why, "recommended_action": action}
    return narrative


@dataclass(frozen=True)
class AuditFinding:
    """One observation as the client receives it: the fact, its proof, and what to do."""

    code: str
    kind: str
    subject: Optional[str]
    evidence: str
    why_it_matters: Optional[str]
    recommended_action: str
    # True when the action below is the hand-written fallback rather than generated prose. The
    # client does not render this differently today; it exists so that "how often does the model
    # actually contribute?" is answerable from stored data rather than by guessing.
    is_template: bool


def assemble(
    observations: List[AuditObservation], narrative: Dict[str, Dict[str, str]]
) -> List[AuditFinding]:
    """Deterministic observations joined to whatever prose survived validation.

    Iterates the observations, never the narrative -- so an observation the model ignored still
    reaches the student, and a narrative entry with no observation has nothing to attach to.
    """
    findings: List[AuditFinding] = []
    for obs in observations:
        written = narrative.get(_narrative_key(obs.code, obs.subject)) if narrative else None
        findings.append(
            AuditFinding(
                code=obs.code,
                kind=obs.kind,
                subject=obs.subject,
                evidence=obs.evidence,
                why_it_matters=written["why_it_matters"] if written else None,
                recommended_action=written["recommended_action"] if written else obs.fallback_action,
                is_template=written is None,
            )
        )
    return findings
