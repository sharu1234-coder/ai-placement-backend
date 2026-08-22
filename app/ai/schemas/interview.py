from pydantic import BaseModel


class GeneratedInterviewQuestion(BaseModel):
    question_text: str
    based_on: str  # short tag, e.g. "DSA", "Project: Inventory Tracker", "HR: teamwork"


class GeneratedInterviewEvaluation(BaseModel):
    """Text-mode evaluation. speaking_pace/filler_word_rate are voice-only (Stage 10) and
    excluded here -- set to None server-side rather than asked of the model."""

    technical_correctness: float
    relevance: float
    completeness: float
    reasoning: float
    structure: float
    clarity: float
    grammar: float
    role_jd_coverage: float
    overall_score: float
    feedback: str
