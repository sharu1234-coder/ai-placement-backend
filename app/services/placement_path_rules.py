"""Deterministic, AI-free core of the Placement Path feature: which tracks apply to a given
resume skill pool, what topics each track contains and in what order, how a topic's lock
state and priority are computed, and what "next best action" to surface. Every function here
is a pure function of its inputs (no DB, no AI, no randomness) so it's directly unit-testable
and produces the exact same tree/priorities/order every time for the same inputs -- the spec
explicitly forbids "random topic ordering" and AI-decided structure, so none of that logic
lives here or anywhere near an AIProvider call."""

import re
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

Priority = Literal["high", "medium", "low"]
EffectiveStatus = Literal["locked", "not_started", "in_progress", "completed"]


@dataclass(frozen=True)
class TopicTemplate:
    topic_key: str
    title: str
    skill_tag: str


@dataclass(frozen=True)
class TrackDef:
    track_key: str
    stage: str
    trigger_keywords: frozenset  # subset of skill_matching.PLACEMENT_TOPIC_KEYWORDS-style terms
    assessment_type: Optional[str]  # None => manual-only stage, no practice blueprint ever
    topics: tuple  # tuple[TopicTemplate, ...], in fixed prerequisite order


@dataclass(frozen=True)
class TopicSpec:
    stage: str
    track_key: str
    topic_key: str
    order_in_track: int
    title: str
    skill_tag: str
    assessment_type: Optional[str]
    global_order: int  # position in the overall pedagogical sequence -- see pick_next_best_action


@dataclass(frozen=True)
class TopicView:
    """What compute_topic_priority/compute_effective_status/pick_next_best_action need to
    know about one topic -- decoupled from the SQLAlchemy row so these functions stay pure
    and DB-free."""

    topic_key: str
    track_key: str
    stage: str
    order_in_track: int
    title: str
    skill_tag: str
    status: str  # not_started/in_progress/completed (the persisted value, never "locked")
    prerequisite_status: Optional[str]  # None if this is the first topic in its track
    global_order: int  # position in build_topic_specs' flattened output -- see pick_next_best_action


@dataclass(frozen=True)
class NextBestActionSpec:
    topic_key: str
    title: str
    reason: str


# Public: the service layer uses this to group topics into stages in catalog order when
# building the API response (only stages with >= 1 topic are ever shown).
STAGE_ORDER = [
    "foundation",
    "core_technical",
    "practice_assessments",
    "interview_prep",
    "final_readiness",
]

# --- Domain tracks: included only if the resume evidences a trigger skill ------------------

_SQL_TRACK = TrackDef(
    track_key="sql",
    stage="core_technical",
    trigger_keywords=frozenset({"sql", "mysql", "postgresql", "postgres", "sqlite", "oracle"}),
    assessment_type="sql",
    topics=(
        TopicTemplate("sql_basics", "SQL Basics", "SQL Basics"),
        TopicTemplate("sql_filtering", "SQL Filtering", "SQL Filtering"),
        TopicTemplate("sql_aggregation", "SQL Aggregation", "SQL Aggregation"),
        TopicTemplate("sql_joins", "SQL JOINs", "SQL JOINs"),
        TopicTemplate("sql_subqueries", "SQL Subqueries", "SQL Subqueries"),
        TopicTemplate("sql_window_functions", "SQL Window Functions", "SQL Window Functions"),
    ),
)

_ML_DATA_SCIENCE_TRACK = TrackDef(
    track_key="ml_data_science",
    stage="core_technical",
    trigger_keywords=frozenset(
        {
            "machine learning", "ml", "ai", "deep learning", "data science", "nlp",
            "natural language processing", "computer vision", "neural networks",
            "pandas", "numpy", "tensorflow", "pytorch", "scikit learn",
            "statistics", "data analysis", "data analytics",
        }
    ),
    assessment_type="technical",
    topics=(
        TopicTemplate("ml_python_libs", "NumPy & Pandas", "ML NumPy Pandas"),
        TopicTemplate("ml_statistics", "Statistics for ML", "ML Statistics"),
        TopicTemplate("ml_data_preprocessing", "Data Preprocessing", "ML Data Preprocessing"),
        TopicTemplate("ml_supervised_learning", "Supervised Learning", "ML Supervised Learning"),
        TopicTemplate("ml_model_evaluation", "Model Evaluation", "ML Model Evaluation"),
        TopicTemplate("ml_interview_prep", "ML Interview Prep", "ML Interview Prep"),
    ),
)

_WEB_BACKEND_TRACK = TrackDef(
    track_key="web_backend",
    stage="core_technical",
    trigger_keywords=frozenset(
        {
            "html", "css", "react", "angular", "vue", "node", "nodejs", "django", "flask",
            "fastapi", "spring", "spring boot", "rest api", "graphql", "microservices",
            "web development",
        }
    ),
    assessment_type="technical",
    topics=(
        TopicTemplate("web_html_css", "HTML & CSS Fundamentals", "Web HTML CSS"),
        TopicTemplate("web_js_fundamentals", "JavaScript Fundamentals", "Web JavaScript"),
        TopicTemplate("web_rest_api_design", "REST API Design", "Web REST API Design"),
        TopicTemplate("web_framework_deep_dive", "Framework Deep Dive", "Web Framework"),
        TopicTemplate("web_db_integration", "Database Integration", "Web Database Integration"),
    ),
)

_CLOUD_DEVOPS_TRACK = TrackDef(
    track_key="cloud_devops",
    stage="core_technical",
    trigger_keywords=frozenset(
        {"aws", "azure", "gcp", "cloud computing", "docker", "kubernetes", "devops", "ci cd"}
    ),
    assessment_type="technical",
    topics=(
        TopicTemplate("cloud_fundamentals", "Cloud Fundamentals", "Cloud Fundamentals"),
        TopicTemplate("cloud_containers", "Containers", "Cloud Containers"),
        TopicTemplate("cloud_orchestration", "Orchestration", "Cloud Orchestration"),
        TopicTemplate("cloud_cicd", "CI/CD Pipelines", "Cloud CI CD"),
    ),
)

_MOBILE_TRACK = TrackDef(
    track_key="mobile",
    stage="core_technical",
    trigger_keywords=frozenset({"android", "ios", "flutter", "react native"}),
    assessment_type="technical",
    topics=(
        TopicTemplate("mobile_fundamentals", "Mobile Fundamentals", "Mobile Fundamentals"),
        TopicTemplate("mobile_ui_navigation", "UI & Navigation", "Mobile UI Navigation"),
        TopicTemplate("mobile_lifecycle_state", "Lifecycle & State", "Mobile Lifecycle State"),
        TopicTemplate(
            "mobile_networking_persistence", "Networking & Persistence", "Mobile Networking Persistence"
        ),
    ),
)

_SECURITY_TESTING_TRACK = TrackDef(
    track_key="security_testing",
    stage="core_technical",
    trigger_keywords=frozenset(
        {"cybersecurity", "network security", "testing", "unit testing", "software testing", "automation testing"}
    ),
    assessment_type="technical",
    topics=(
        TopicTemplate("sec_fundamentals", "Security Fundamentals", "Security Fundamentals"),
        TopicTemplate("sec_testing_types", "Testing Types", "Software Testing Types"),
        TopicTemplate("sec_automation_basics", "Test Automation Basics", "Test Automation"),
    ),
)

_DOMAIN_TRACKS = (
    _SQL_TRACK,
    _ML_DATA_SCIENCE_TRACK,
    _WEB_BACKEND_TRACK,
    _CLOUD_DEVOPS_TRACK,
    _MOBILE_TRACK,
    _SECURITY_TESTING_TRACK,
)

# Fixed detection priority -- deterministic regardless of resume list order. Only the top
# `cap` detected languages become their own Programming Fundamentals track.
_LANGUAGE_PRIORITY = [
    "python", "java", "javascript", "typescript", "c++", "c", "c#",
    "kotlin", "swift", "go", "rust", "scala", "php", "ruby",
]

_PROGRAMMING_FUNDAMENTALS_TOPIC_TEMPLATES = (
    ("prog_syntax_control_flow", "Syntax & Control Flow"),
    ("prog_data_types_collections", "Data Types & Collections"),
    ("prog_functions_oop", "Functions & OOP"),
    ("prog_error_handling", "Error Handling"),
)


def _slug(language: str) -> str:
    return language.replace("+", "p").replace("#", "s")


def _programming_fundamentals_track(language: str) -> TrackDef:
    label = {"c++": "C++", "c#": "C#", "c": "C"}.get(language, language.title())
    slug = _slug(language)
    topics = tuple(
        TopicTemplate(f"{key}_{slug}", f"{label} {title}", f"{label} {title}")
        for key, title in _PROGRAMMING_FUNDAMENTALS_TOPIC_TEMPLATES
    )
    return TrackDef(
        track_key=f"programming_fundamentals_{slug}",
        stage="foundation",
        trigger_keywords=frozenset(),
        assessment_type="coding",
        topics=topics,
    )


# --- Baseline tracks: included whenever >= 1 domain track matched --------------------------

_DSA_TRACK = TrackDef(
    track_key="dsa",
    stage="foundation",
    trigger_keywords=frozenset(),  # baseline, not resume-gated
    assessment_type="coding",
    topics=(
        TopicTemplate("dsa_complexity_basics", "Complexity Basics", "DSA Complexity"),
        TopicTemplate("dsa_arrays_strings", "Arrays & Strings", "DSA Arrays Strings"),
        TopicTemplate("dsa_searching_sorting", "Searching & Sorting", "DSA Searching Sorting"),
        TopicTemplate("dsa_linked_lists", "Linked Lists", "DSA Linked Lists"),
        TopicTemplate("dsa_stacks_queues", "Stacks & Queues", "DSA Stacks Queues"),
        TopicTemplate("dsa_trees", "Trees", "DSA Trees"),
        TopicTemplate("dsa_graphs", "Graphs", "DSA Graphs"),
        TopicTemplate("dsa_dynamic_programming", "Dynamic Programming", "DSA Dynamic Programming"),
    ),
)

_APTITUDE_TRACK = TrackDef(
    track_key="aptitude_reasoning",
    stage="foundation",
    trigger_keywords=frozenset(),
    assessment_type="aptitude",
    topics=(
        TopicTemplate("apt_quantitative", "Quantitative Aptitude", "Quantitative Aptitude"),
        TopicTemplate("apt_logical_reasoning", "Logical Reasoning", "Logical Reasoning"),
        TopicTemplate("apt_verbal_ability", "Verbal Ability", "Verbal Ability"),
        TopicTemplate("apt_data_interpretation", "Data Interpretation", "Data Interpretation"),
    ),
)

_PRACTICE_TRACK = TrackDef(
    track_key="practice_assessments",
    stage="practice_assessments",
    trigger_keywords=frozenset(),
    assessment_type=None,  # manual-only: these are checkpoints, not single-skill practice
    topics=(
        TopicTemplate("practice_full_mock", "Full Mock Assessment", "Placement Practice"),
        TopicTemplate("practice_targeted_weak_areas", "Targeted Weak-Area Drills", "Placement Practice"),
    ),
)

_INTERVIEW_PREP_TRACK = TrackDef(
    track_key="interview_prep",
    stage="interview_prep",
    trigger_keywords=frozenset(),
    assessment_type=None,
    topics=(
        TopicTemplate("interview_behavioral_basics", "Behavioral Interview Basics", "Interview Prep"),
        TopicTemplate(
            "interview_technical_communication", "Technical Communication", "Interview Prep"
        ),
        TopicTemplate("interview_mock_practice", "Mock Interview Practice", "Interview Prep"),
    ),
)

_FINAL_READINESS_TRACK = TrackDef(
    track_key="final_readiness",
    stage="final_readiness",
    trigger_keywords=frozenset(),
    assessment_type=None,
    topics=(
        TopicTemplate("final_resume_polish", "Resume Polish", "Placement Readiness"),
        TopicTemplate("final_full_length_dry_run", "Full-Length Dry Run", "Placement Readiness"),
    ),
)

_BASELINE_TRACKS = (_DSA_TRACK, _APTITUDE_TRACK, _PRACTICE_TRACK, _INTERVIEW_PREP_TRACK, _FINAL_READINESS_TRACK)

_DOMAIN_TRACK_RANK = {track.track_key: i for i, track in enumerate(_DOMAIN_TRACKS)}
_BASELINE_TRACK_RANK = {track.track_key: i for i, track in enumerate(_BASELINE_TRACKS)}
_LANGUAGE_RANK = {lang: i for i, lang in enumerate(_LANGUAGE_PRIORITY)}
_TRACKS_BY_KEY = {track.track_key: track for track in (*_DOMAIN_TRACKS, *_BASELINE_TRACKS)}
_LANGUAGE_BY_SLUG = {_slug(lang): lang for lang in _LANGUAGE_PRIORITY}


def _track_def_for(track_key: str) -> Optional[TrackDef]:
    """Resolves any track_key -- including a dynamically-named programming_fundamentals_{lang}
    one -- back to its TrackDef, independent of which subset of tracks a particular student's
    path actually contains. Used both for rank lookups and for reconstructing a stored topic's
    static definition (title/skill_tag/assessment_type) after generation time."""
    if track_key.startswith("programming_fundamentals_"):
        slug = track_key[len("programming_fundamentals_") :]
        lang = _LANGUAGE_BY_SLUG.get(slug)
        return _programming_fundamentals_track(lang) if lang else None
    return _TRACKS_BY_KEY.get(track_key)


def track_rank(track_key: str) -> int:
    """A track's fixed position in the overall pedagogical sequence (languages, then domain
    tracks in _DOMAIN_TRACKS order, then baseline tracks in _BASELINE_TRACKS order) --
    independent of which subset of tracks a particular student's path actually contains.
    Used to reconstruct global_order for topics read back from the DB, where build_topic_specs'
    original assembly order isn't itself persisted (only stage/track_key/order_in_track are)."""
    if track_key.startswith("programming_fundamentals_"):
        slug = track_key[len("programming_fundamentals_") :]
        lang = _LANGUAGE_BY_SLUG.get(slug)
        return _LANGUAGE_RANK[lang] if lang else len(_LANGUAGE_PRIORITY)
    if track_key in _DOMAIN_TRACK_RANK:
        return 100 + _DOMAIN_TRACK_RANK[track_key]
    if track_key in _BASELINE_TRACK_RANK:
        return 200 + _BASELINE_TRACK_RANK[track_key]
    return 999


def topic_spec_for(track_key: str, topic_key: str) -> Optional[TopicSpec]:
    """Looks up a stored topic's static catalog definition by (track_key, topic_key), for use
    after generation time when only these two stable strings are persisted (not the full
    resume-driven build context). Returns None if the track/topic isn't in the current
    catalog at all (e.g. the catalog changed after this topic was created)."""
    track = _track_def_for(track_key)
    if track is None:
        return None
    for i, template in enumerate(track.topics, start=1):
        if template.topic_key == topic_key:
            return TopicSpec(
                stage=track.stage,
                track_key=track.track_key,
                topic_key=template.topic_key,
                order_in_track=i,
                title=template.title,
                skill_tag=template.skill_tag,
                assessment_type=track.assessment_type,
                global_order=track_rank(track_key) * 1000 + i,
            )
    return None


def match_domain_tracks(resume_skills: List[str]) -> Dict[str, List[str]]:
    """track_key -> the resume skill(s) that triggered its inclusion. A domain track is
    included iff at least one of its trigger keywords matches a resume skill via the same
    token-boundary allowlist matching used everywhere else in this codebase."""
    matched: Dict[str, List[str]] = {}
    for track in _DOMAIN_TRACKS:
        hits = [skill for skill in resume_skills if _matches_any_keyword(skill, track.trigger_keywords)]
        if hits:
            matched[track.track_key] = hits
    return matched


def _matches_any_keyword(skill: str, keywords: frozenset) -> bool:
    """Track-specific token-subset match. Deliberately does NOT also require
    is_placement_topic(skill) to pass -- every track's trigger_keywords is a curated subset
    of the shared PLACEMENT_TOPIC_KEYWORDS allowlist already, so that would be redundant, and
    worse, a maintenance trap: a future trigger keyword added here but not to the shared list
    would silently stop matching if this also gated on is_placement_topic."""
    skill_lower = skill.strip().lower()
    return any(_token_subset_match(skill_lower, kw) for kw in keywords)


def _token_subset_match(skill_lower: str, keyword: str) -> bool:
    skill_tokens = set(_split_tokens(skill_lower))
    keyword_tokens = set(_split_tokens(keyword))
    if not skill_tokens or not keyword_tokens:
        return False
    return keyword_tokens <= skill_tokens or skill_tokens <= keyword_tokens


def _split_tokens(text: str) -> List[str]:
    # Keeps "+"/"#" attached to their token (not stripped as separators) -- otherwise "C",
    # "C++", and "C#" all collapse to the identical token {"c"} and become indistinguishable
    # to _token_subset_match, so a resume listing "C#" could get matched to a "C" or "C++"
    # Programming Fundamentals track instead of (or as well as) "C#". No other keyword in this
    # module's vocabulary contains "+" or "#", so this only sharpens the C-family distinction.
    return [t for t in re.split(r"[^a-z0-9+#]+", text.lower()) if t]


def detect_primary_languages(resume_skills: List[str], cap: int = 2) -> List[str]:
    """Deterministic fixed-priority-order language detection, capped at `cap` -- avoids the
    tree exploding into many redundant per-language tracks on a resume listing 5+ languages."""
    lower_skills = [s.strip().lower() for s in resume_skills]
    detected = [lang for lang in _LANGUAGE_PRIORITY if any(_token_subset_match(s, lang) for s in lower_skills)]
    return detected[:cap]


def build_topic_specs(matched_domain_tracks: Dict[str, List[str]], languages: List[str]) -> List[TopicSpec]:
    """Pure, deterministic assembly of the full topic tree. Baseline tracks (DSA, Aptitude,
    Practice & Assessments, Interview Prep, Final Readiness) are included iff at least one
    domain track matched -- mirrors the spec's own worked example (DSA becomes high priority
    for a Python+ML student with no direct DSA evidence, because it's a near-universal
    placement requirement once the student is on a technical track at all)."""
    if not matched_domain_tracks:
        return []

    tracks: List[TrackDef] = []
    for lang in languages:
        tracks.append(_programming_fundamentals_track(lang))
    for track in _DOMAIN_TRACKS:
        if track.track_key in matched_domain_tracks:
            tracks.append(track)
    tracks.extend(_BASELINE_TRACKS)

    specs: List[TopicSpec] = []
    global_order = 0
    for track in tracks:
        for i, template in enumerate(track.topics, start=1):
            specs.append(
                TopicSpec(
                    stage=track.stage,
                    track_key=track.track_key,
                    topic_key=template.topic_key,
                    order_in_track=i,
                    title=template.title,
                    skill_tag=template.skill_tag,
                    assessment_type=track.assessment_type,
                    global_order=global_order,
                )
            )
            global_order += 1
    return specs


def compute_effective_status(topic_status: str, prerequisite_status: Optional[str]) -> EffectiveStatus:
    """A topic with no prerequisite (first in its track) is never locked. Otherwise it's
    locked unless its prerequisite is completed -- regardless of the topic's own persisted
    status (the service layer is responsible for never letting a locked topic's status
    advance past not_started in the first place; this function just computes the display
    overlay)."""
    if prerequisite_status is None or prerequisite_status == "completed":
        return topic_status  # type: ignore[return-value]
    return "locked"


def compute_topic_priority(
    topic: TopicView,
    is_first_incomplete_in_track: bool,
    track_size: int,
    weak_skill_tags: frozenset,
) -> Priority:
    """First matching rule wins. AI is never consulted -- priority is fully deterministic."""
    effective = compute_effective_status(topic.status, topic.prerequisite_status)
    if effective == "completed":
        return "low"
    if topic.skill_tag in weak_skill_tags:
        return "high"
    if is_first_incomplete_in_track:
        return "high"
    if track_size > 0 and topic.order_in_track > (2 * track_size) / 3:
        return "low"
    return "medium"


def compute_progress(topics: List[TopicView]) -> tuple:
    completed = sum(1 for t in topics if t.status == "completed")
    return completed, len(topics)


def pick_next_best_action(
    topics: List[TopicView], priorities: Dict[str, Priority]
) -> Optional[NextBestActionSpec]:
    """First actionable (not locked, not completed) topic, preferring priority=="high", tied
    by global_order (the overall pedagogical sequence build_topic_specs assembled -- languages,
    then each matched domain track, then DSA/Aptitude/Practice/Interview/Final Readiness).
    Never returns a locked topic. Deliberately NOT tie-broken by stage+track_key+order_in_track
    directly: track_key is a free string and sorting by it alphabetically would, e.g., put
    "aptitude_reasoning" ahead of "dsa" and "programming_fundamentals_python" purely by
    spelling -- global_order already encodes the intended pedagogical order, so it's the only
    tie-break needed."""
    actionable = [
        t
        for t in topics
        if compute_effective_status(t.status, t.prerequisite_status) in ("not_started", "in_progress")
    ]
    if not actionable:
        return None

    def sort_key(t: TopicView):
        is_high = 0 if priorities.get(t.topic_key) == "high" else 1
        return (is_high, t.global_order)

    chosen = sorted(actionable, key=sort_key)[0]
    if chosen.status == "in_progress":
        reason = f"You're partway through {chosen.title} -- pick up where you left off."
    elif chosen.prerequisite_status == "completed":
        track_label = chosen.track_key.replace("_", " ")
        reason = f"You've completed the prerequisite for {chosen.title} -- it's the next step in {track_label}."
    else:
        reason = f"{chosen.title} is a foundational placement-prep topic to start with."
    return NextBestActionSpec(topic_key=chosen.topic_key, title=chosen.title, reason=reason)
