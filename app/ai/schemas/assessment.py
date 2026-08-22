from typing import List, Literal, Optional

from pydantic import BaseModel


class GeneratedQuestion(BaseModel):
    """What the AI provider generates for a single question. skill_tag, difficulty, and
    source are known server-side (they're inputs to the prompt, not outputs), so they're
    excluded here the same way AIParsedJD excludes detected_role_id."""

    type: Literal["mcq", "coding", "sql", "case_study", "short_answer"]
    prompt: str
    options: Optional[List[str]] = None
    expected_answer_or_rubric: str
    explanation: str


class AnswerGrade(BaseModel):
    is_correct: bool
    score: float
    feedback: str
