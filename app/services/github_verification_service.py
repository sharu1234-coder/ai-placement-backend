"""GitHub Project Verification (Phase 8) -- scoped down from the full spec deliberately:
unauthenticated, public-repos-only inspection via GitHub's public REST API. No OAuth, no
token storage, avoiding the encrypted-token-storage security burden the spec itself flags.

This is real but LIMITED signal: a public repo existing, having a README, and its primary
language matching the student's declared stack is evidence the project is genuinely public
and plausible -- never proof of code quality or that the student personally wrote it. Every
result is phrased as "evidence detected", exactly per the spec's own "never claim 100%
verified" language. Nothing here calls AI or touches Evidence Engine's scoring -- see the
router/service docstring for why that boundary is deliberate.
"""

import datetime
import re
from typing import Optional, Tuple

import httpx
from pydantic import BaseModel

from app.services.skill_matching import skill_present

_GITHUB_API_BASE = "https://api.github.com"
_TIMEOUT = 10.0
_README_SNIPPET_LENGTH = 300

# https://github.com/{owner}/{repo}[.git][/] -- owner/repo names are limited to
# alphanumerics, hyphens, underscores, and dots by GitHub's own naming rules.
_REPO_URL_PATTERN = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


class GitHubVerificationError(Exception):
    pass


class InvalidRepoUrlError(GitHubVerificationError):
    pass


class GitHubApiError(GitHubVerificationError):
    """GitHub could not be reached, or returned something other than a clean 200/404
    (typically the unauthenticated rate limit, 60 requests/hour/IP). Deliberately distinct
    from "repo not found" (an ordinary 404) -- never fabricate a "not found" result when the
    real reason is a transient failure or rate limit; the student needs to know to retry,
    not be told their repo doesn't exist."""


class GitHubEvidence(BaseModel):
    repo_url: str
    owner: str
    repo_name: str
    repo_exists: bool
    primary_language: Optional[str] = None
    has_readme: bool = False
    readme_snippet: Optional[str] = None
    stars: int = 0
    last_pushed_at: Optional[datetime.datetime] = None
    # None when there's nothing to compare against (no declared stack, or repo has no
    # detected language) -- never a fabricated True/False with no real basis.
    matches_declared_stack: Optional[bool] = None
    evidence_label: str
    checked_at: datetime.datetime


def parse_repo_url(repo_url: str) -> Tuple[str, str]:
    match = _REPO_URL_PATTERN.match(repo_url.strip())
    if not match:
        raise InvalidRepoUrlError(
            "Enter a valid public GitHub repository URL, e.g. https://github.com/owner/repo"
        )
    return match.group(1), match.group(2)


def _headers(accept: str = "application/vnd.github+json") -> dict:
    # GitHub's API rejects unauthenticated requests with no User-Agent at all -- this is a
    # fixed, honest identifier for this app's own server, not a per-user value.
    return {"Accept": accept, "User-Agent": "AI-Placement-OS"}


def _parse_github_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_repository(repo_url: str, stack_summary: Optional[str] = None) -> GitHubEvidence:
    owner, repo_name = parse_repo_url(repo_url)
    now = datetime.datetime.now(datetime.timezone.utc)

    try:
        repo_resp = httpx.get(f"{_GITHUB_API_BASE}/repos/{owner}/{repo_name}", headers=_headers(), timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise GitHubApiError("Could not reach GitHub right now. Please try again.") from exc

    if repo_resp.status_code == 404:
        return GitHubEvidence(
            repo_url=repo_url,
            owner=owner,
            repo_name=repo_name,
            repo_exists=False,
            evidence_label="No public repository found at this URL.",
            checked_at=now,
        )
    if repo_resp.status_code == 403:
        raise GitHubApiError("GitHub's rate limit was hit -- please try again in a few minutes.")
    if repo_resp.status_code != 200:
        raise GitHubApiError(f"GitHub returned an unexpected error (status {repo_resp.status_code}).")

    repo_data = repo_resp.json()

    has_readme = False
    readme_snippet = None
    try:
        readme_resp = httpx.get(
            f"{_GITHUB_API_BASE}/repos/{owner}/{repo_name}/readme",
            headers=_headers(accept="application/vnd.github.raw+json"),
            timeout=_TIMEOUT,
        )
        if readme_resp.status_code == 200:
            has_readme = True
            readme_snippet = readme_resp.text[:_README_SNIPPET_LENGTH]
    except httpx.HTTPError:
        pass  # README is a nice-to-have; a failure here never blocks the main repo result

    primary_language = repo_data.get("language")
    matches_declared_stack = (
        skill_present(primary_language, [stack_summary]) if primary_language and stack_summary else None
    )

    if has_readme and primary_language:
        evidence_label = "Public repository found -- well-documented with a detected primary language."
    elif has_readme:
        evidence_label = "Public repository found -- has a README."
    else:
        evidence_label = "Public repository found -- no README detected."

    return GitHubEvidence(
        repo_url=repo_url,
        owner=owner,
        repo_name=repo_name,
        repo_exists=True,
        primary_language=primary_language,
        has_readme=has_readme,
        readme_snippet=readme_snippet,
        stars=repo_data.get("stargazers_count", 0),
        last_pushed_at=_parse_github_datetime(repo_data.get("pushed_at")),
        matches_declared_stack=matches_declared_stack,
        evidence_label=evidence_label,
        checked_at=now,
    )
