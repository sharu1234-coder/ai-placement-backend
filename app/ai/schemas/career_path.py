from typing import List, Literal

from pydantic import BaseModel, Field


class GeneratedRoleOverview(BaseModel):
    short_explanation: str = Field(max_length=500)
    responsibilities: List[str]
    core_skills: List[str]
    tools: List[str]
    difficulty: Literal["beginner-friendly", "moderate", "challenging"]
    entry_level_expectations: str = Field(max_length=400)
    interview_focus: List[str]
    portfolio_expectations: str = Field(max_length=400)


class GeneratedRoleSkill(BaseModel):
    skill_key: str
    title: str
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"


class GeneratedRolePhase(BaseModel):
    """phase_key must be one of the 5 fixed phase keys (foundations, core_skills, projects,
    interview_prep, job_ready) -- the service layer validates this and silently drops any
    phase whose key doesn't match, the same "never trust an unrecognized key" enforcement
    used everywhere else in this codebase's AI overlays."""

    phase_key: str
    skills: List[GeneratedRoleSkill]


class GeneratedRoleCatalog(BaseModel):
    """A full role definition for a role not found in the curated catalog. Deliberately does
    NOT include project templates -- AI-assisted roles use a generic, role-name-parameterized
    project fallback instead (see career_path/ai_generation.py), keeping AI surface area
    small and predictable rather than validating a second, more complex generated shape."""

    overview: GeneratedRoleOverview
    phases: List[GeneratedRolePhase]


class GeneratedSkillExplanation(BaseModel):
    """One skill item's "why it matters" text. skill_key must echo one of the keys the
    prompt supplied verbatim -- the service layer validates this and never trusts an
    unrecognized or missing key (falls back to a deterministic template sentence instead)."""

    skill_key: str
    why_it_matters: str = Field(max_length=400)


class GeneratedCareerPathExplanations(BaseModel):
    explanations: List[GeneratedSkillExplanation]
