from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ScoreBand:
    key: str
    min_score: int
    label: str


# Single source of truth for category weights -- every category score and the overall
# score are derived only through this dict, never a hardcoded number elsewhere in the
# engine. Sums to 1.0 (asserted in tests/services/test_ats_scoring_config.py).
CATEGORY_WEIGHTS = {
    "parsing": 0.20,
    "structure": 0.15,
    "content": 0.15,
    "skills": 0.15,
    "experience": 0.15,
    "projects": 0.10,
    "contact": 0.05,
    "readability": 0.05,
}

CATEGORY_LABELS = {
    "parsing": "ATS Compatibility",
    "structure": "Structure",
    "content": "Content Quality",
    "skills": "Skills",
    "experience": "Experience",
    "projects": "Projects",
    "contact": "Contact Info",
    "readability": "Readability",
}

# Ordered highest-first so band_for_score can walk it in one pass. Copy stays supportive
# even at the bottom band -- students see this, not a recruiter or an internal dashboard.
SCORE_BANDS: List[ScoreBand] = [
    ScoreBand("excellent", 90, "Excellent"),
    ScoreBand("very_good", 80, "Very Good"),
    ScoreBand("good", 70, "Good"),
    ScoreBand("needs_improvement", 60, "Needs Improvement"),
    ScoreBand("needs_attention", 0, "Needs Attention"),
]


def band_for_score(score: int) -> ScoreBand:
    for band in SCORE_BANDS:
        if score >= band.min_score:
            return band
    return SCORE_BANDS[-1]  # unreachable given min_score=0 on the last band, kept for safety
