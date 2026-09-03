import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ProjectBuildStageRead(BaseModel):
    stage_key: str
    title: str
    order_index: int
    status: str  # not_started | in_progress | completed
    guidance_text: str
    notes: Optional[str] = None
    started_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None


class ProjectBuildListItemRead(BaseModel):
    id: int
    title: str
    target_role_name: Optional[str] = None
    status: str  # not_started | in_progress | completed
    created_at: datetime.datetime
    completed_at: Optional[datetime.datetime] = None


class GitHubEvidenceRead(BaseModel):
    repo_url: str
    owner: str
    repo_name: str
    repo_exists: bool
    primary_language: Optional[str] = None
    has_readme: bool = False
    readme_snippet: Optional[str] = None
    stars: int = 0
    last_pushed_at: Optional[datetime.datetime] = None
    matches_declared_stack: Optional[bool] = None
    evidence_label: str
    checked_at: datetime.datetime


class ProjectBuildRead(ProjectBuildListItemRead):
    stack_summary: Optional[str] = None
    stages: List[ProjectBuildStageRead] = []
    github_evidence: Optional[GitHubEvidenceRead] = None


class CreateProjectBuildRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    stack_summary: Optional[str] = Field(default=None, max_length=300)


class CompleteStageRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=2000)


class CheckGithubRequest(BaseModel):
    repo_url: str = Field(min_length=1, max_length=500)


class ProjectProofRead(BaseModel):
    resume_bullet: str
    readme_markdown: str
    explanation_60s: str
    explanation_5min: str
    defense_questions: List[str]
