from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class IssueDraft:
    category: str
    severity: str  # "high" | "medium" | "low"
    title: str
    description: str
    how_to_fix: str
    points_recoverable: int  # how much fixing this would add back to potential_score


@dataclass
class CategoryResult:
    category: str
    score: int  # 0-100, already clamped
    summary: str
    strengths: List[str] = field(default_factory=list)
    improvements: List[str] = field(default_factory=list)
    how_to_improve: List[str] = field(default_factory=list)
    issues: List[IssueDraft] = field(default_factory=list)


@dataclass
class SectionFinding:
    section: str
    status: str  # "complete" | "good" | "needs_improvement" | "missing"
    content: Optional[str] = None


def clamp_score(score: float) -> int:
    return max(0, min(100, round(score)))
