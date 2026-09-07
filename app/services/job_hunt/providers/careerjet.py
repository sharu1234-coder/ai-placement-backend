"""Careerjet adapter.

Documentation this was written against (fetched 2026-09-07, not recalled):
  * https://www.careerjet.co.in/partners/api -- endpoint
    https://search.api.careerjet.net/v4/query (HTTP GET); `user_ip` and `user_agent` are
    mandatory on every call; optional `keywords`, `location`, `locale_code` (default en_GB,
    en_IN for India), `page` (1-10), `page_size` (1-100, default 20), `sort`
    (relevance/date/salary), `contract_type`, `work_hours`, `radius`, `fragment_size`.
    Per-job response fields: title, company, date, description, locations, salary,
    salary_currency_code, salary_min, salary_max, salary_type, url.
  * https://github.com/careerjet/careerjet-api-client-python -- the official client, which
    additionally requires `affid` (from a Careerjet partner account) and `url`.

Two things worth flagging rather than papering over.

First, an unresolved discrepancy: the v4 endpoint documentation and the official Python client
disagree on parameter names (`page_size`/`contract_type`/`work_hours` versus
`pagesize`/`contracttype`/`contractperiod`) and on whether `affid` is mandatory. The client
targets the older API. This adapter implements the v4 names from the endpoint documentation
and sends `affid` whenever it is configured. Which spelling the live endpoint actually honours
is exactly the kind of thing a fixture cannot settle, so it is listed in the audit report as
requiring a live smoke test before release, not asserted here.

Second, a privacy consequence the owner should decide on knowingly: Careerjet contractually
requires the *end user's* IP address and user agent on every query. That means each student's
IP is disclosed to Careerjet on every search. It is a documented condition of using the API,
not an implementation choice, but it conflicts with "minimize third-party sharing" (spec
section 14) and so is surfaced as an owner decision rather than quietly implemented.
"""

import datetime
import hashlib
import logging
from typing import Any, Optional

from app.services.job_hunt.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    JobProvider,
    JobProviderAuthError,
    JobProviderNotConfiguredError,
    JobProviderRateLimitError,
    JobProviderUnavailableError,
    ProviderJob,
    ProviderSearchQuery,
    ProviderSearchResult,
    get_with_one_retry,
)

logger = logging.getLogger(__name__)

_ENDPOINT = "https://search.api.careerjet.net/v4/query"
_MAX_PAGE_SIZE = 100
# Documented ceiling: pages beyond 10 are not served.
_MAX_PAGE = 10

_CONTRACT_TYPE = {"permanent": "p", "contract": "c", "temporary": "t", "internship": "i"}
_WORK_HOURS = {"full_time": "f", "part_time": "p"}


class CareerjetJobProvider(JobProvider):
    name = "careerjet"
    attribution_text = "Jobs by Careerjet"
    attribution_url = "https://www.careerjet.co.in"
    cache_ttl = datetime.timedelta(hours=6)

    def __init__(
        self,
        affid: Optional[str],
        locale_code: str = "en_IN",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._affid = affid
        self._locale_code = locale_code
        self._timeout = timeout_seconds

    def search(self, query: ProviderSearchQuery) -> ProviderSearchResult:
        if not self._affid:
            raise JobProviderNotConfiguredError(
                "Careerjet is selected as JOB_PROVIDER but CAREERJET_AFFID is unset"
            )
        # Mandatory per Careerjet's documentation. Failing loudly beats sending a placeholder
        # IP: a fake value would be a silent terms violation that still returned results, so
        # nothing would ever reveal the breach.
        if not query.end_user_ip or not query.end_user_agent:
            raise JobProviderNotConfiguredError(
                "Careerjet requires the end user's IP and user agent on every query, and "
                "neither was supplied by the caller"
            )

        params: dict[str, Any] = {
            "locale_code": self._locale_code,
            "affid": self._affid,
            "user_ip": query.end_user_ip,
            "user_agent": query.end_user_agent,
            "page": min(max(query.page, 1), _MAX_PAGE),
            "page_size": min(query.page_size, _MAX_PAGE_SIZE),
        }
        if query.keywords:
            params["keywords"] = query.keywords
        if query.location:
            params["location"] = query.location
        if query.employment_type in _CONTRACT_TYPE:
            params["contract_type"] = _CONTRACT_TYPE[query.employment_type]
        if query.employment_type in _WORK_HOURS:
            params["work_hours"] = _WORK_HOURS[query.employment_type]
        # Careerjet has no salary-floor or max-age filter, so those preferences cannot be
        # pushed down to the provider. They are applied by the matching engine instead, which
        # is why the engine scores rather than filters -- see job_hunt/matching.py.

        # One bounded retry on a transport failure or 5xx; never on 4xx. See
        # base.get_with_one_retry for why the line is drawn there.
        response = get_with_one_retry(
            _ENDPOINT, params=params, timeout=self._timeout, provider_name="Careerjet"
        )

        if response.status_code in (401, 403):
            raise JobProviderAuthError(
                f"Careerjet rejected the configured affiliate id ({response.status_code})"
            )
        if response.status_code == 429:
            raise JobProviderRateLimitError("Careerjet rate limit exhausted (429)")
        if response.status_code >= 400:
            raise JobProviderUnavailableError(f"Careerjet returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise JobProviderUnavailableError("Careerjet returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise JobProviderUnavailableError("Careerjet returned an unexpected payload shape")

        # Careerjet signals application-level failures in the body with a 200 status, so the
        # HTTP checks above are not sufficient on their own.
        if payload.get("type") == "ERROR":
            raise JobProviderUnavailableError("Careerjet reported an error response")

        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list):
            raise JobProviderUnavailableError("Careerjet payload had no 'jobs' list")

        jobs = []
        for raw in raw_jobs:
            parsed = self._parse_job(raw)
            if parsed is not None:
                jobs.append(parsed)
        if len(jobs) != len(raw_jobs):
            logger.warning(
                "Careerjet: skipped %d of %d results that could not be parsed",
                len(raw_jobs) - len(jobs),
                len(raw_jobs),
            )

        hits = payload.get("hits")
        return ProviderSearchResult(jobs=jobs, total_available=hits if isinstance(hits, int) else None)

    def _parse_job(self, raw: Any) -> Optional[ProviderJob]:
        if not isinstance(raw, dict):
            return None
        url = raw.get("url")
        title = raw.get("title")
        if not isinstance(url, str) or not url or not isinstance(title, str) or not title:
            return None

        return ProviderJob(
            provider=self.name,
            provider_job_id=_stable_id(url),
            url=url,
            title=title,
            company=raw.get("company") if isinstance(raw.get("company"), str) else None,
            # v4 returns a single pre-joined "locations" string, not a list.
            location=raw.get("locations") if isinstance(raw.get("locations"), str) else None,
            # Careerjet exposes no work-mode or seniority field. Not inferred -- see the
            # ProviderJob docstring on why a guess must not be presented as provider data.
            work_mode=None,
            employment_type=None,
            seniority=None,
            salary_min=_as_int(raw.get("salary_min")),
            salary_max=_as_int(raw.get("salary_max")),
            salary_currency=raw.get("salary_currency_code")
            if isinstance(raw.get("salary_currency_code"), str)
            else None,
            salary_period=_salary_period(raw.get("salary_type")),
            # Careerjet's salary figures come from the advert itself, never modelled.
            salary_is_predicted=False,
            description=raw.get("description") if isinstance(raw.get("description"), str) else None,
            posted_at=_parse_date(raw.get("date")),
        )


def _stable_id(url: str) -> str:
    """Careerjet's v4 response carries no per-advert id, but dedupe and the
    (provider, provider_job_id) uniqueness constraint both need a stable key.

    A SHA-256 prefix of the canonical URL is derived, not invented: the same advert yields the
    same id on every fetch, and two different adverts cannot collide in practice. Truncated to
    32 hex characters to stay inside the column width while leaving collision probability
    negligible at this scale.
    """
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]


def _salary_period(value: Any) -> Optional[str]:
    """Careerjet's salary_type is a coarse period label. Anything unrecognized stays None
    rather than being bucketed into "year", which would misstate an hourly rate by ~2000x."""
    if not isinstance(value, str):
        return None
    return {"Y": "year", "M": "month", "W": "week", "D": "day", "H": "hour"}.get(value.upper()[:1])


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> Optional[datetime.datetime]:
    """Careerjet returns "YYYY-MM-DD HH:MM:SS" rather than ISO 8601 with a T separator."""
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None
