from typing import List, Optional

from pydantic import BaseModel


class JDRequirementItem(BaseModel):
    value: str
    is_explicit: bool  # True = literally stated in the JD text; False = AI-inferred


class AIParsedJD(BaseModel):
    """What the AI provider actually generates. Excludes detected_role_id -- the model has
    no knowledge of our internal Role catalog, so role detection is resolved server-side
    (see jd_service.detect_role) by matching role_title against it."""

    role_title: str
    required_skills: List[JDRequirementItem]
    preferred_skills: List[JDRequirementItem]
    responsibilities: List[str]
    tools_technologies: List[str]
    experience_expectation: Optional[str] = None
    education_eligibility: Optional[str] = None
    other_signals: List[str]
    parsing_confidence: float
