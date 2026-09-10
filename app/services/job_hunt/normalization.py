"""Turning messy human and provider text into stable keys the matching engine can compare.

Everything here is deterministic and AI-free, deliberately. The same discipline
app/services/weakness_service.py already states applies: *which* skills a student has and
*which* a job wants is our own product logic computed from owned data, not something delegated
to a model. AI is allowed to help extract text elsewhere in the app; it is never what decides
a match, because a match has to be reproducible and explainable (spec section 6).

Skill comparison reuses skill_matching's token-subset rule rather than reimplementing it, so
"React" still matches "react.js" here exactly as it does in the resume/roadmap code, and a
future fix to that rule fixes every feature at once.
"""

import ipaddress
import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from app.services.skill_matching import PLACEMENT_TOPIC_KEYWORDS, skill_present

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9+#.]+")

# Canonical spellings for skills students and adverts write many ways. Deliberately small and
# curated: every entry is a genuine alias, never a broadening ("java" does NOT map to
# "javascript", the classic bug that would credit a Java developer with JavaScript). Anything
# absent falls through to its own lowercased form, which is a safe default -- an unmapped skill
# simply matches only itself.
SKILL_SYNONYMS: dict[str, str] = {
    "js": "javascript",
    "node": "node.js",
    "nodejs": "node.js",
    "reactjs": "react",
    "react.js": "react",
    "postgres": "postgresql",
    "psql": "postgresql",
    "k8s": "kubernetes",
    "ml": "machine learning",
    "dl": "deep learning",
    "ai": "artificial intelligence",
    "nlp": "natural language processing",
    "cv": "computer vision",
    "oops": "oop",
    "object oriented programming": "oop",
    "dsa": "data structures and algorithms",
    "data structures": "data structures and algorithms",
    "rdbms": "dbms",
    "database management system": "dbms",
    "os": "operating systems",
    "operating system": "operating systems",
    "cn": "computer networks",
    "computer network": "computer networks",
    "golang": "go",
    "c sharp": "c#",
    "csharp": "c#",
    "cpp": "c++",
    "c plus plus": "c++",
    "spring boot": "spring",
    "springboot": "spring",
    "tf": "tensorflow",
    "sklearn": "scikit learn",
    "scikit-learn": "scikit learn",
    "ci/cd": "ci cd",
    "cicd": "ci cd",
    "gcp": "google cloud",
    "aws cloud": "aws",
    "restful api": "rest api",
    "rest apis": "rest api",
    "restapi": "rest api",
}

# Role families this app already understands, mapped from the many titles adverts use. Used
# only to decide whether a listing's title is *the same kind of job* the student is targeting;
# it never invents a target the student did not choose.
ROLE_FAMILY_ALIASES: dict[str, tuple[str, ...]] = {
    "software_engineer": (
        "software engineer", "software developer", "sde", "sde 1", "sde i", "backend engineer",
        "back end developer", "backend developer", "full stack developer", "fullstack developer",
        "full stack engineer", "application developer", "programmer analyst", "software engineer 1",
        "member of technical staff", "software development engineer",
    ),
    "frontend_engineer": (
        "frontend engineer", "front end developer", "frontend developer", "ui developer",
        "react developer", "web developer", "angular developer",
    ),
    "data_analyst": (
        "data analyst", "business analyst", "analytics analyst", "reporting analyst",
        "business intelligence analyst", "bi analyst", "mis executive",
    ),
    "data_scientist": (
        "data scientist", "machine learning engineer", "ml engineer", "ai engineer",
        "research scientist", "applied scientist", "deep learning engineer",
    ),
    "data_engineer": (
        "data engineer", "etl developer", "big data engineer", "analytics engineer",
        "data platform engineer",
    ),
    "devops_engineer": (
        "devops engineer", "site reliability engineer", "sre", "platform engineer",
        "cloud engineer", "infrastructure engineer", "build and release engineer",
    ),
    "qa_engineer": (
        "qa engineer", "test engineer", "quality assurance engineer", "sdet",
        "automation test engineer", "software tester",
    ),
    "mobile_engineer": (
        "android developer", "ios developer", "mobile developer", "android engineer",
        "flutter developer", "react native developer", "mobile application developer",
    ),
    "product_manager": (
        "product manager", "associate product manager", "apm", "product owner",
        "technical product manager",
    ),
    "ui_ux_designer": (
        "ui ux designer", "ux designer", "ui designer", "product designer",
        "interaction designer", "visual designer",
    ),
    "cybersecurity_engineer": (
        "security engineer", "cybersecurity analyst", "information security analyst",
        "soc analyst", "penetration tester", "security analyst",
    ),
}

_EXPERIENCE_LEVELS = ("fresher", "junior", "mid", "senior", "lead")

# Seniority words that appear in advert titles. Matched on word boundaries so "senior" is not
# found inside an unrelated word, and so "sr." is caught alongside "senior".
_SENIORITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("intern", r"\bintern(ship)?\b"),
    ("fresher", r"\b(fresher|graduate|entry[ -]level|trainee|campus)\b"),
    ("senior", r"\b(senior|sr\.?|staff|principal|architect)\b"),
    ("lead", r"\b(lead|manager|head|director|vp)\b"),
)


def skill_key(raw: str) -> str:
    """Canonical form of one skill: lowercased, punctuation-collapsed, synonym-resolved.

    Keeps "+", "#" and "." because they are load-bearing in real skill names (c++, c#,
    node.js); stripping them would collapse "c++" and "c" into the same key and credit a C
    programmer with C++.
    """
    if not raw:
        return ""
    cleaned = " ".join(t for t in _TOKEN_SPLIT_RE.split(raw.strip().lower()) if t)
    cleaned = cleaned.strip(". ")
    return SKILL_SYNONYMS.get(cleaned, cleaned)


def normalize_skills(raw_skills: Iterable[str]) -> list[str]:
    """Canonicalize, drop blanks, and de-duplicate while preserving first-seen order.

    Order matters downstream: the UI shows "top matched skills", and a stable order keeps that
    list from reshuffling between two identical searches.
    """
    seen: dict[str, None] = {}
    for raw in raw_skills or []:
        if not isinstance(raw, str):
            continue
        key = skill_key(raw)
        if key:
            seen.setdefault(key, None)
    return list(seen)


def role_family(title: Optional[str]) -> Optional[str]:
    """Map a free-text job title onto one of this app's role families, or None.

    None is a real answer meaning "we do not recognize this title", and the matching engine
    treats it as an unknown factor rather than as a mismatch. Guessing a family from a title we
    do not know would silently mis-rank every result for that student.
    """
    if not title:
        return None
    normalized = " ".join(t for t in _TOKEN_SPLIT_RE.split(title.strip().lower()) if t)
    if not normalized:
        return None
    for family, aliases in ROLE_FAMILY_ALIASES.items():
        for alias in aliases:
            # Substring on a space-padded string gives a word-boundary match without a regex
            # per alias: " sde " matches "sde 1" but never "sdet".
            if f" {alias} " in f" {normalized} ":
                return family
    return None


def seniority_from_title(title: Optional[str]) -> Optional[str]:
    """Best-effort seniority read from an advert title, or None when the title says nothing.

    Deliberately ordered most-specific first: "Senior Software Engineer Intern" is an
    internship, and checking "intern" before "senior" is what makes that come out right.
    """
    if not title:
        return None
    lowered = title.lower()
    for level, pattern in _SENIORITY_PATTERNS:
        if re.search(pattern, lowered):
            return level
    return None


def work_mode_from_text(*texts: Optional[str]) -> Optional[str]:
    """Detect remote/hybrid/on-site from advert text.

    Neither Adzuna nor Careerjet exposes a work-mode field, so for this one attribute the text
    is the only available source. It is kept honest three ways: only explicit phrases count
    (never a bare "home"), hybrid is checked before remote because "hybrid remote" is hybrid,
    and a miss returns None so the UI omits work mode entirely rather than assuming on-site.
    """
    joined = " ".join(t.lower() for t in texts if t)
    if not joined:
        return None
    if re.search(r"\bhybrid\b", joined):
        return "hybrid"
    if re.search(r"\b(fully remote|remote work|work from home|wfh|remote position|100% remote|remote)\b", joined):
        return "remote"
    if re.search(r"\b(on[- ]site|onsite|in[- ]office|work from office|wfo)\b", joined):
        return "onsite"
    return None


def extract_job_skills(*texts: Optional[str]) -> list[str]:
    """Which known placement/tech skills a job advert actually names.

    Restricted to PLACEMENT_TOPIC_KEYWORDS -- the curated allowlist this codebase already
    shares across the Placement Alarm and Placement Path -- rather than any capitalized token.
    An open-ended extractor would pull company names, city names and boilerplate into the
    "required skills" list, and every one of those would then show up as a fabricated "missing
    skill" the student is told to go and learn.
    """
    joined = " ".join(t.lower() for t in texts if t)
    if not joined:
        return []
    tokens = [t for t in _TOKEN_SPLIT_RE.split(joined) if t]
    token_set = set(tokens)
    # Bigrams and trigrams, so multi-word entries ("machine learning", "data structures and
    # algorithms") are findable at all -- a token set alone can only ever match single words.
    phrases = token_set | {
        " ".join(tokens[i : i + n]) for n in (2, 3, 4) for i in range(len(tokens) - n + 1)
    }
    found: dict[str, None] = {}
    for keyword in PLACEMENT_TOPIC_KEYWORDS:
        if keyword in phrases:
            found.setdefault(skill_key(keyword), None)
    return list(found)


def skills_intersection(student_skills: list[str], job_skills: list[str]) -> tuple[list[str], list[str]]:
    """Split a job's skills into (matched, missing) against the student's.

    Uses skill_matching.skill_present, so the token-subset semantics are identical to the rest
    of the app rather than a second, subtly different notion of "has this skill".
    """
    matched: list[str] = []
    missing: list[str] = []
    for job_skill in job_skills:
        if skill_present(job_skill, student_skills):
            matched.append(job_skill)
        else:
            missing.append(job_skill)
    return matched, missing


def normalize_experience_level(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    lowered = value.strip().lower()
    if lowered in _EXPERIENCE_LEVELS:
        return lowered
    return {"entry": "fresher", "graduate": "fresher", "0-1": "fresher", "intern": "fresher"}.get(lowered)


# -- URL safety ---------------------------------------------------------------------------
# Only these schemes may ever reach the Android client. The client opens the URL in a browser
# intent, so a "javascript:" or "intent:" URL from a compromised or sloppy provider would be
# handed straight to the device -- this is the choke point that stops that (spec section 14).
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# Hosts that would make an outbound fetch reach back into our own infrastructure. This app
# never server-side-fetches a listing URL today, but the allowlist is applied at storage time
# so that a future "fetch full description" feature cannot become an SSRF by inheriting
# unvalidated URLs from rows written now.
_BLOCKED_URL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "metadata.google.internal"})


def safe_external_url(url: Optional[str]) -> Optional[str]:
    """Return the URL only if it is a plain http(s) link to a public host, else None.

    Returning None (rather than raising) lets the caller drop a single bad listing without
    failing the whole search, which is the same one-bad-row-does-not-fail-the-page policy the
    provider adapters use.
    """
    if not url or not isinstance(url, str):
        return None
    candidate = url.strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        return None
    host = (parsed.hostname or "").lower().strip("[]")
    if not host or host in _BLOCKED_URL_HOSTS:
        return None
    if _is_non_public_address(host):
        return None
    return candidate


def _is_non_public_address(host: str) -> bool:
    """True for anything that is not a public DNS name.

    Prefix matching on the dotted-quad form (the first version of this check) let six things
    through, each of which resolves to somewhere it should not:

        http://2130706433/          127.0.0.1 written as a single decimal
        http://0x7f000001/          the same address in hex
        http://0177.0.0.1/          the same address with an octal first octet
        http://[::1]/               IPv6 loopback -- caught by name, but only by name
        http://[::ffff:127.0.0.1]/  IPv4-mapped loopback
        http://[fd00::1]/           IPv6 unique-local, the v6 equivalent of 10.0.0.0/8

    `ipaddress` parses the canonical forms and answers the question properly, which covers
    loopback, private, link-local, reserved and unspecified in both families at once.

    What this deliberately does NOT do is resolve DNS. `http://internal.corp.example/` may
    well point at 10.0.0.5, and this function accepts it. That is a considered limit, not an
    oversight:

      * Resolving here would prove nothing about where a later request actually goes. The name
        can resolve differently a second later -- that is DNS rebinding, and a check at storage
        time is precisely the shape it defeats. A validator that looks thorough and is not is
        worse than one whose limits are written down.
      * It would put a DNS lookup in the hot path of every listing on every search page.

    The guarantee this function offers is therefore exact: *an address literal* cannot name a
    non-public host. If a server-side fetch of listing URLs is ever added, the check it needs
    is at connect time against the address actually connected to, not here.

    What `ipaddress` will not parse is the deliberately obfuscated forms: it rejects a leading
    zero as ambiguous and does not accept hex at all, so `0177.0.0.1` and `0x7f000001` would
    fall through as if they were hostnames. So anything that is not a parseable address must
    still look like a real DNS name to pass, and the test for that is the one property every
    public domain has and every numeric-obfuscated address lacks: a dotted name whose last
    label contains a letter. No real job advert URL fails it -- every public TLD is alphabetic.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # `is_global` already covers loopback, private, link-local, reserved, unspecified and
        # the CGNAT range, in both families. Multicast is checked separately because it does
        # not: ipaddress reports 224.0.0.1 -- "all hosts on this subnet" -- as global, and it
        # is plainly not somewhere a job advert lives.
        return not address.is_global or address.is_multicast
    labels = host.split(".")
    if len(labels) < 2:
        # A single label is either an intranet name or an obfuscated address; neither is a
        # public job advert.
        return True
    return not any(character.isalpha() for character in labels[-1])
