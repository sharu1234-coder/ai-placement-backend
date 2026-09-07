"""Removing the same advert appearing twice in one result set.

Aggregators re-list the same vacancy from several sources, so a single search routinely
returns the identical job two or three times. The rule (spec section 4) is provider IDs first,
then a *conservative* title/company/location/URL comparison.

Conservative is the operative word, and it points one way: a false merge is much worse than a
missed one. Wrongly collapsing two adverts hides a real job the student could have applied to,
and they never find out it existed. Wrongly keeping two near-identical rows is a cosmetic
annoyance they can see and ignore. So every rule here demands exact agreement on normalized
values rather than fuzzy similarity -- no edit distance, no token overlap thresholds, nothing
that merges "Software Engineer" with "Software Engineer II".
"""

import re
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from app.services.job_hunt.providers.base import ProviderJob

_WHITESPACE_RE = re.compile(r"\s+")


def _norm_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", value.strip().lower())


def canonical_url(url: Optional[str]) -> str:
    """URL with scheme/host lowercased, tracking query parameters and fragment removed.

    The query string is dropped entirely rather than filtered param-by-param: on every provider
    seen here the identifying part of a listing URL is in the path, and keeping unknown query
    parameters would defeat the comparison for the exact case it exists to catch.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return ""
    if not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def _content_key(job: ProviderJob) -> Optional[Tuple[str, str, str]]:
    """Identity by what the advert says, for jobs whose ids differ.

    Requires title AND company AND location to all be present and to agree exactly once
    normalized. Returning None when any is missing is deliberate: two adverts that both lack a
    company are not thereby the same job, and treating a shared blank as a match is precisely
    the over-merge this module refuses to make.
    """
    title = _norm_text(job.title)
    company = _norm_text(job.company)
    location = _norm_text(job.location)
    if not title or not company or not location:
        return None
    return (title, company, location)


def deduplicate(jobs: Iterable[ProviderJob]) -> List[ProviderJob]:
    """Return the input with duplicates removed, keeping the first occurrence of each.

    First-wins preserves the provider's own relevance ordering, which is information we did not
    compute and should not discard.

    Three passes of increasing looseness, each still exact-match:
      1. (provider, provider_job_id) -- the same advert from the same provider.
      2. canonical URL -- the same advert reached through two providers or two search pages.
      3. (title, company, location) -- the same vacancy syndicated under different URLs.
    """
    seen_ids: set = set()
    seen_urls: set = set()
    seen_content: set = set()
    result: List[ProviderJob] = []

    for job in jobs:
        identity = (job.provider, job.provider_job_id)
        if identity in seen_ids:
            continue

        url_key = canonical_url(job.url)
        if url_key and url_key in seen_urls:
            continue

        content_key = _content_key(job)
        if content_key is not None and content_key in seen_content:
            continue

        seen_ids.add(identity)
        if url_key:
            seen_urls.add(url_key)
        if content_key is not None:
            seen_content.add(content_key)
        result.append(job)

    return result
