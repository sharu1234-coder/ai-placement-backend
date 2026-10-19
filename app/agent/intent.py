"""Deterministic intent classification and entity extraction.

Rule-based, and that is the design rather than a placeholder. Three reasons:

* **VA-6 must run without a model.** No production LLM has been selected (VA-3's gate is still
  open) and the cost policy is ₹0-first, so an orchestrator whose first step required an
  inference would not work in this environment at all.
* **The tool policy requires a closed set.** What can execute has to be knowable in advance.
  A model choosing freely among capabilities is the opposite of that, so the model's role --
  where one exists -- is to pick from `Intent`, never to name a tool.
* **It is testable.** A rule either fires or it does not, so every intent has a deterministic
  test rather than one that depends on which model answered.

An LLM may refine an `UNCLEAR` result (see `orchestrator`), and its answer is validated back
against this same closed set before anything runs.
"""

from __future__ import annotations

import re
from typing import Optional

from app.agent.contracts import Entities, Intent

#: Patterns per intent, checked in the order listed below. Order matters where phrases overlap:
#: "what do fresher jobs need" is about role requirements, not about finding fresher jobs, so
#: the requirements rule is checked first.
_RULES: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (
        Intent.LOCATION_COMPARISON,
        # Bare "compare" counts. The first version required "compare ... in/between" or a
        # "vs", which missed "compare Pune for data analyst" and sent it to UNCLEAR -- a
        # clarifying question about which cities to compare is far more useful there than
        # "I did not follow that".
        (r"\bcompare\b", r"\b(vs|versus)\b", r"\bbetter (city|place|location)\b"),
    ),
    (
        # Checked before SKILL_GAP_VS_MARKET: "what should I learn in 30 days" is a plan
        # request, and answering it with a bare gap list would drop the horizon the student
        # actually asked about.
        Intent.CAREER_PLAN,
        (
            r"\b(7|30|60|90)[ -]?day\b",
            r"\b(road ?map|plan|schedule|timeline)\b.*\b(career|job|placement|role|skills?|months?|weeks?|days?)\b",
            # Both orders, because students write the horizon on either side of the noun:
            # "three month plan" as readily as "plan for three months". Days are included so
            # "45 day plan" is recognised as a plan request with an unsupported horizon and
            # gets a clarifying question, rather than falling through to "I did not follow
            # that" -- the request was perfectly clear, only the horizon was not one we have.
            r"\b(weeks?|months?|days?)\b.*\b(road ?map|plan|schedule|timeline)\b",
            r"\b(make|build|give|create|draw up)\b.*\b(road ?map|plan)\b",
            r"\bwhat should i do (over|in|for) the next\b",
        ),
    ),
    (
        Intent.ROLE_SELECTION,
        (
            r"\bwhich (role|job|career|path|track)\b",
            r"\b(what|which).*\b(suits?|suited|right for) me\b",
            r"\b(should i (go for|target|aim for|pick|choose))\b",
            r"\b(career|role) (choice|options?|selection|direction)\b",
            r"\bmy (career )?goals?\b",
        ),
    ),
    (
        Intent.SKILL_GAP_VS_MARKET,
        (
            r"\b(gap|gaps|missing)\b.*\b(market|employers?|jobs?|industry)\b",
            r"\bwhat (should|do) i learn\b",
            r"\bam i ready for\b",
            r"\bwhat am i missing\b",
        ),
    ),
    (
        Intent.ROLE_REQUIREMENTS,
        (
            r"\bwhat (do|does).*\b(require|need|ask for|look for)\b",
            r"\b(requirements?|expectations?)\b",
            r"\bskills? (needed|required|for)\b",
            # "what skills do backend engineers need" reached none of the rules above, because
            # they expect "what do ... need" or "skills needed". The lookahead keeps it off
            # "what skills do I have", which is SKILL_STATUS and is checked much later.
            r"\bwhat skills?\b(?!.*\bdo i\b).*\b(need|require|ask|expect|look for)",
        ),
    ),
    (
        Intent.SKILL_DEMAND,
        (r"\b(in )?demand\b", r"\bmost (asked|common|popular)\b", r"\btrending\b", r"\bhow many jobs? (want|ask)\b"),
    ),
    (
        Intent.COMPANY_HIRING,
        (r"\bwho(?:'s| is| are)? hiring\b", r"\bwhich compan(y|ies)\b", r"\bcompanies hiring\b"),
    ),
    (
        Intent.FRESHER_JOBS,
        (r"\b(fresher|freshers|entry.level|graduate|internship|intern)\b.*\b(jobs?|roles?|openings?|hiring)\b",
         r"\b(jobs?|roles?|openings?)\b.*\b(fresher|freshers|entry.level|graduate)\b"),
    ),
    (
        Intent.JOB_SEARCH,
        (r"\b(find|show|any|search|look for|list)\b.*\b(jobs?|roles?|openings?|vacanc)", r"\bjobs? (in|near|for)\b"),
    ),
    (Intent.WEAKNESSES, (r"\bweak(ness|nesses)?\b", r"\bstruggl", r"\bbad at\b", r"\bimprove\b")),
    (
        Intent.READINESS,
        # "is my resume ready" used to land here, because `ready` is checked before the resume
        # rules below and matched first. The student asked about their resume, and a readiness
        # answer about their target role is a confident answer to a different question. The
        # lookahead hands those back to RESUME_STATUS without reordering the table, which
        # would have let `\bresume\b` start swallowing genuine readiness questions instead.
        (
            r"^(?!.*\b(resume|cv|ats)\b).*\bread(y|iness)\b",
            r"\bhow prepared\b",
            r"\bcoverage\b",
            r"\btwin\b",
        ),
    ),
    (Intent.INTERVIEW_HISTORY, (r"\binterview", r"\bmock\b")),
    (Intent.RESUME_STATUS, (r"\bresume\b", r"\bcv\b", r"\bats\b")),
    (Intent.SAVED_JOBS, (r"\bsaved jobs?\b", r"\bapplications?\b", r"\bapplied\b", r"\btracker\b")),
    (Intent.TODAY_PLAN, (r"\btoday\b", r"\bmission\b", r"\bwhat (should|do) i do\b", r"\bnext step\b")),
    (Intent.PROGRESS, (r"\bprogress\b", r"\bscore\b", r"\bhow am i doing\b", r"\bdashboard\b")),
    (Intent.SKILL_STATUS, (r"\bmy skills?\b", r"\bskill passport\b", r"\bwhat skills do i\b")),
    (Intent.PROFILE_STATUS, (r"\bmy profile\b", r"\bmy (college|degree|branch)\b", r"\bwho am i\b")),
)

#: Recognised, and recognised as something VA-6 will not do. Matched *before* the rules above
#: so that "apply to this job" is refused rather than answered as a job search -- a request to
#: act must not be quietly reinterpreted as a request to look.
_OUT_OF_SCOPE = (
    r"\b(apply|submit|send)\b.*\b(application|job|resume|cv)\b",
    r"\b(book|schedule|cancel|delete|update|change|set)\b",
    r"\bsave (this|that|it)\b",
    r"\bremind me\b",
)

_COMPILED = tuple((intent, tuple(re.compile(p, re.IGNORECASE) for p in patterns)) for intent, patterns in _RULES)
_COMPILED_OUT = tuple(re.compile(p, re.IGNORECASE) for p in _OUT_OF_SCOPE)


def classify(text: str) -> Intent:
    """Map a request onto the closed intent set.

    Returns `UNCLEAR` rather than guessing when nothing matches. An orchestrator that guessed
    would run tools against a question nobody asked, and the student would get a confident
    answer to the wrong thing -- which is harder to notice than a clarifying question.
    """
    if not text or not text.strip():
        return Intent.UNCLEAR
    for pattern in _COMPILED_OUT:
        if pattern.search(text):
            return Intent.OUT_OF_SCOPE
    for intent, patterns in _COMPILED:
        if any(pattern.search(text) for pattern in patterns):
            return intent
    return Intent.UNCLEAR


# --------------------------------------------------------------------------- entities

#: Indian metros and tech hubs plus "remote". A closed list, because an open-ended place
#: extractor would send arbitrary user text to a provider as a geography filter -- and a
#: provider that does not recognise it silently widens the search instead of narrowing it,
#: which looks like a result about the asked-for city and is not.
_KNOWN_LOCATIONS = (
    "bangalore", "bengaluru", "hyderabad", "pune", "chennai", "mumbai", "delhi", "noida",
    "gurgaon", "gurugram", "kolkata", "ahmedabad", "jaipur", "kochi", "coimbatore",
    "indore", "chandigarh", "trivandrum", "thiruvananthapuram", "remote",
)

#: Role families and the nouns that follow them, composed rather than written out twice, so the
#: "as a <role>" form and the bare form cannot drift apart.
_ROLE_FAMILY = (
    r"(?:software|backend|frontend|full[ -]?stack|data|ml|devops|qa|android|ios|cloud|security)"
)
#: Plural accepted, because "what skills do data engineers need" is how the question is asked,
#: and the singular-only pattern sent it to a clarifying question about which role was meant --
#: a question whose answer was already in the sentence. Normalised back to the singular by
#: `_singular` before it reaches a provider query, so one role does not become two cache keys.
_ROLE_NOUN = r"(?:engineer|developer|analyst|scientist)s?"
_PLURAL_NOUN = re.compile(r"\b(engineer|developer|analyst|scientist)s\b", re.IGNORECASE)


def _singular(role: str) -> str:
    return _PLURAL_NOUN.sub(lambda m: m.group(1), role)
_ROLE_PATTERNS = (
    rf"\b(?:as|for|a|an)\s+((?:senior |junior |lead )?{_ROLE_FAMILY}\s+{_ROLE_NOUN})\b",
    rf"\b({_ROLE_FAMILY}\s+{_ROLE_NOUN})\b",
    r"\b(sde|sdet|data analyst|data scientist|business analyst|product manager)\b",
)
_COMPILED_ROLES = tuple(re.compile(p, re.IGNORECASE) for p in _ROLE_PATTERNS)

_EXPERIENCE = (
    (("fresher", "freshers", "entry level", "entry-level", "graduate", "intern", "internship"), "fresher"),
    (("junior",), "junior"),
    (("senior",), "senior"),
    (("lead",), "lead"),
)

def _first_skill(text: str) -> Optional[str]:
    """The first known skill named in the request.

    Delegates to the existing `normalization.extract_job_skills` rather than keeping a second
    vocabulary. That function is already restricted to the codebase's curated placement
    keyword allowlist -- the same allowlist the world layer uses to read adverts -- so the
    agent and the market data agree on what counts as a skill. A private list here would drift,
    and the drift would surface as the agent asking about a skill the market extractor cannot
    find, which reads as "no demand" rather than as a vocabulary mismatch.
    """
    from app.services.job_hunt import normalization

    found = normalization.extract_job_skills(text)
    return found[0] if found else None


#: Horizons the plan builder has a milestone shape for. A number outside this set is left
#: unset rather than rounded: rounding "45 days" to 30 or 60 would hand back a schedule the
#: student did not ask for and would look deliberate.
_HORIZON = re.compile(r"\b(7|30|60|90)\s*[- ]?\s*(?:day|days)\b", re.IGNORECASE)
#: Also accept the month forms students actually use, mapped to the same four.
_HORIZON_MONTHS = ((r"\b(?:one|1)\s*month\b", 30), (r"\b(?:two|2)\s*months?\b", 60),
                   (r"\b(?:three|3)\s*months?\b", 90), (r"\b(?:a|one|1)\s*week\b", 7))
_WEEKLY_HOURS = re.compile(
    r"\b(\d{1,2})\s*(?:hours?|hrs?)\s*(?:a|per|each|every)?\s*(?:week|wk)\b", re.IGNORECASE
)


def _horizon(text: str) -> Optional[int]:
    # The time budget is removed before the horizon is looked for. "10 hours a week" contains
    # "a week", which the month/week table would otherwise read as a 7-day horizon -- turning
    # a statement about how much time the student has into a statement about how long the plan
    # should be. Found by a smoke test on exactly that sentence.
    without_budget = _WEEKLY_HOURS.sub(" ", text)
    match = _HORIZON.search(without_budget)
    if match:
        return int(match.group(1))
    for pattern, days in _HORIZON_MONTHS:
        if re.search(pattern, without_budget, re.IGNORECASE):
            return days
    return None


def _weekly_hours(text: str) -> Optional[int]:
    match = _WEEKLY_HOURS.search(text)
    if not match:
        return None
    hours = int(match.group(1))
    # Out-of-range is dropped rather than clamped. "200 hours a week" is not a time budget,
    # and clamping it to 80 would invent a constraint the student never stated.
    return hours if 1 <= hours <= Entities.MAX_WEEKLY_HOURS else None


def extract(text: str) -> Entities:
    """Pull the bounded set of entities a tool argument can use.

    Everything here is matched against a closed list or a fixed pattern. Nothing is passed
    through from free text unvalidated, because the only consumer is a provider query and a
    provider's interpretation of an unrecognised value is not predictable.
    """
    lowered = (text or "").lower()

    found_locations = tuple(
        dict.fromkeys(
            name.title() if name != "remote" else "remote"
            for name in _KNOWN_LOCATIONS
            if re.search(rf"\b{re.escape(name)}\b", lowered)
        )
    )

    role: Optional[str] = None
    for pattern in _COMPILED_ROLES:
        match = pattern.search(text or "")
        if match:
            role = _singular(match.group(1).strip())
            break

    experience: Optional[str] = None
    for needles, level in _EXPERIENCE:
        if any(re.search(rf"\b{re.escape(n)}\b", lowered) for n in needles):
            experience = level
            break

    return Entities(
        role=role,
        location=found_locations[0] if found_locations else None,
        locations=found_locations,
        skill=_first_skill(text or ""),
        experience_level=experience,
        horizon_days=_horizon(text or ""),
        weekly_hours=_weekly_hours(text or ""),
    )
