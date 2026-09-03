from pydantic import BaseModel


class GeneratedRecruiterAttackQuestion(BaseModel):
    question_text: str
    based_on: str  # short tag, e.g. "Your specific role", "Scaling claim"


class GeneratedRecruiterAttackEvaluation(BaseModel):
    technical_defense: float
    project_ownership: float
    specificity: float
    communication: float
    overall_score: float
    feedback: str
