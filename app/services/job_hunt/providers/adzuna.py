"""Adzuna adapter.

Documentation this was written against (fetched 2026-09-07, not recalled):
  * https://developer.adzuna.com/overview -- root URL https://api.adzuna.com/v1/api, auth is
    the `app_id` + `app_key` query pair on every request, search path is
    /jobs/{country}/search/{page}.
  * https://developer.adzuna.com/docs/terms_of_service -- default quota "25 hits per minute,
    250 hits per day, 1000 hits per week, 2500 hits per month"; attribution must label each
    advert "Jobs by Adzuna" with "Jobs" hyperlinked; commercial use beyond a 14-day
    evaluation period requires written consent.

That last clause is a real constraint on shipping this, not a footnote: AI Placement OS is a
commercial product, so using Adzuna in production needs an agreement with Adzuna first. It is
implemented here because it is the best-documented candidate and the owner may well obtain
that agreement -- but the gate is recorded in the audit report as an owner decision, and
nothing about having written this adapter implies permission to use it.

India is supported as country code "in"; the country is configurable rather than hardcoded so
the same adapter serves whichever market the owner licenses.
"""

import datetime
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

_ROOT_URL = "https://api.adzuna.com/v1/api"
# Adzuna caps results_per_page; 50 is the documented maximum for the search endpoint.
_MAX_PAGE_SIZE = 50


class AdzunaJobProvider(JobProvider):
    name = "adzuna"
    attribution_text = "Jobs by Adzuna"
    attribution_url = "https://www.adzuna.co.in"
    # Well inside anything Adzuna's terms require, and short enough that a filled or withdrawn
    # advert does not linger in front of students for a working day.
    cache_ttl = datetime.timedelta(hours=6)

    def __init__(
        self,
        app_id: Optional[str],
        app_key: Optional[str],
        country: str = "in",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._country = country
        self._timeout = timeout_seconds

    def search(self, query: ProviderSearchQuery) -> ProviderSearchResult:
        if not self._app_id or not self._app_key:
            raise JobProviderNotConfiguredError(
                "Adzuna is selected as JOB_PROVIDER but ADZUNA_APP_ID/ADZUNA_APP_KEY are unset"
            )

        params: dict[str, Any] = {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "results_per_page": min(query.page_size, _MAX_PAGE_SIZE),
            "content-type": "application/json",
        }
        if query.keywords:
            params["what"] = query.keywords
        if query.location:
            params["where"] = query.location
        if query.max_days_old is not None:
            params["max_days_old"] = query.max_days_old
        if query.salary_min is not None:
            params["salary_min"] = query.salary_min
        # Adzuna expresses these as presence flags ("1"), not as values -- omitting the key
        # means "no preference", which is why this is not a plain assignment.
        if query.employment_type == "full_time":
            params["full_time"] = 1
        elif query.employment_type == "part_time":
            params["part_time"] = 1
        elif query.employment_type == "contract":
            params["contract"] = 1
        elif query.employment_type == "internship":
            # Adzuna has no internship flag. Falling back to a keyword is a real behaviour
            # difference, so it is done explicitly and visibly rather than silently dropping
            # the student's filter and returning unfiltered results as if it had been applied.
            params["what"] = f"{query.keywords} internship".strip() if query.keywords else "internship"

        url = f"{_ROOT_URL}/jobs/{self._country}/search/{max(query.page, 1)}"
        # One bounded retry on a transport failure or 5xx; never on 4xx. See
        # base.get_with_one_retry for why the line is drawn there.
        response = get_with_one_retry(
            url, params=params, timeout=self._timeout, provider_name="Adzuna"
        )

        if response.status_code in (401, 403):
            raise JobProviderAuthError(
                f"Adzuna rejected the configured credentials ({response.status_code})"
            )
        if response.status_code == 429:
            raise JobProviderRateLimitError("Adzuna rate limit or quota exhausted (429)")
        if response.status_code >= 400:
            raise JobProviderUnavailableError(f"Adzuna returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise JobProviderUnavailableError("Adzuna returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise JobProviderUnavailableError("Adzuna returned an unexpected payload shape")

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise JobProviderUnavailableError("Adzuna payload had no 'results' list")

        jobs = []
        for raw in raw_results:
            parsed = self._parse_job(raw)
            # One malformed advert must not fail the whole search -- the student loses that
            # row, not the page. Counted in the log so a provider that starts returning junk
            # is visible rather than silently thinning results.
            if parsed is not None:
                jobs.append(parsed)
        if len(jobs) != len(raw_results):
            logger.warning(
                "Adzuna: skipped %d of %d results that could not be parsed",
                len(raw_results) - len(jobs),
                len(raw_results),
            )

        total = payload.get("count")
        return ProviderSearchResult(
            jobs=jobs, total_available=total if isinstance(total, int) else None
        )

    def _parse_job(self, raw: Any) -> Optional[ProviderJob]:
        if not isinstance(raw, dict):
            return None
        # id, redirect_url and title are the identity trio -- without any one of them the row
        # cannot be deduplicated, linked to, or displayed, so it is dropped rather than
        # patched up with a placeholder.
        job_id = raw.get("id")
        url = raw.get("redirect_url")
        title = raw.get("title")
        if not job_id or not isinstance(url, str) or not isinstance(title, str):
            return None

        company = raw.get("company")
        company_name = company.get("display_name") if isinstance(company, dict) else None
        location = raw.get("location")
        location_name = location.get("display_name") if isinstance(location, dict) else None

        # Documented as the string "0"/"1" rather than a boolean, and absent entirely when the
        # salary is advertised rather than modelled.
        salary_is_predicted = str(raw.get("salary_is_predicted")) == "1"

        return ProviderJob(
            provider=self.name,
            provider_job_id=str(job_id),
            url=url,
            title=title,
            company=company_name,
            location=location_name,
            # Adzuna does not expose a work-mode field at all. Left None rather than inferred
            # from the description, which would put a guess in front of the student as fact.
            work_mode=None,
            employment_type=_employment_type(raw),
            seniority=None,
            salary_min=_as_int(raw.get("salary_min")),
            salary_max=_as_int(raw.get("salary_max")),
            # The API is per-country and this deployment's country determines the currency;
            # Adzuna does not return a currency code per advert.
            salary_currency="INR" if self._country == "in" else None,
            salary_period="year",
            salary_is_predicted=salary_is_predicted,
            description=raw.get("description") if isinstance(raw.get("description"), str) else None,
            posted_at=_parse_created(raw.get("created")),
        )


def _employment_type(raw: dict) -> Optional[str]:
    """Adzuna splits this across two independent fields: contract_time (full_time/part_time)
    and contract_type (permanent/contract). Neither is always present."""
    contract_time = raw.get("contract_time")
    if contract_time in ("full_time", "part_time"):
        return contract_time
    if raw.get("contract_type") == "contract":
        return "contract"
    return None


def _as_int(value: Any) -> Optional[int]:
    """Adzuna returns salaries as JSON numbers, often floats (500000.0). None and unparseable
    values stay None -- a salary we could not read is unknown, not zero."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_created(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str):
        return None
    try:
        # Documented as ISO 8601. Python's fromisoformat handles the "+00:00" form; the "Z"
        # suffix is normalized first because it did not parse before Python 3.11 and this
        # keeps the adapter independent of the interpreter version.
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Stored as naive UTC to match every other DateTime column in this schema.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed
