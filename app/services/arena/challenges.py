"""Placement Arena's challenge catalog -- a static, code-defined list, same shape as
career_path.role_catalog's ROLE_FAMILIES. Each challenge maps to exactly one real skill/
assessment_type pair that arena_service feeds into a single-section AssessmentBlueprint, so
every challenge is answered by the same question-generation/grading engine every other
assessment already uses. This is a timed-mode + personal-best wrapper, not a new question
bank -- adding a challenge here never requires new question content.
"""

from typing import Dict, List, NamedTuple, Optional


class ArenaChallenge(NamedTuple):
    key: str
    title: str
    category: str  # sql | python | aptitude | debugging | dsa
    skill: str  # fed into the Assessment engine as the section's skill/skill_tag
    assessment_type: str  # BlueprintSection.assessment_type
    question_count: int
    time_limit_seconds: int
    description: str


ARENA_CHALLENGES: Dict[str, ArenaChallenge] = {
    "sql_sprint": ArenaChallenge(
        key="sql_sprint", title="SQL Sprint", category="sql", skill="SQL", assessment_type="sql",
        question_count=5, time_limit_seconds=300,
        description="Five timed SQL questions -- joins, aggregates, and query design.",
    ),
    "python_challenge": ArenaChallenge(
        key="python_challenge", title="Python Challenge", category="python", skill="Python",
        assessment_type="technical", question_count=5, time_limit_seconds=300,
        description="Five timed Python questions covering core language and problem-solving.",
    ),
    "aptitude_blitz": ArenaChallenge(
        key="aptitude_blitz", title="Aptitude Blitz", category="aptitude", skill="Aptitude",
        assessment_type="aptitude", question_count=8, time_limit_seconds=240,
        description="Eight quick quantitative and logical reasoning questions against the clock.",
    ),
    "debug_hunt": ArenaChallenge(
        key="debug_hunt", title="Debug Hunt", category="debugging", skill="Debugging",
        assessment_type="technical", question_count=5, time_limit_seconds=360,
        description="Five timed questions on spotting and fixing broken code.",
    ),
    "dsa_dash": ArenaChallenge(
        key="dsa_dash", title="DSA Dash", category="dsa", skill="DSA", assessment_type="coding",
        question_count=5, time_limit_seconds=420,
        description="Five timed data structures and algorithms questions.",
    ),
}


def get_challenge(key: str) -> Optional[ArenaChallenge]:
    return ARENA_CHALLENGES.get(key)


def list_challenges() -> List[ArenaChallenge]:
    return list(ARENA_CHALLENGES.values())
