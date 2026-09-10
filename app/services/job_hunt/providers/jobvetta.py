"""Jobvetta adapter.

The contract this was written against, exactly as supplied by the owner and not extended:

  * Base URL      https://api.jobvetta.com/v1
  * Auth          Authorization: Bearer <JOBVETTA_API_KEY>
  * Search        GET /jobs        parameters: q, location, days, limit  (limit 1-10)
  * Details       GET /jobs/{job_id}
  * Quota         50 requests per API key per UTC day
  * Coverage      India only

Four consequences of that contract shape the code below, and each is a place where the obvious
implementation would have been wrong.

*There are only four search parameters, so there are only four.* Job Hunt's neutral query
carries work mode, employment type, experience level and a salary floor as well. Jobvetta
documents none of them, so none of them are sent -- not under a guessed name, not "just in
case". They are not lost: the matching engine already scores against those preferences rather
than filtering on them, which is why an unfilterable preference degrades into a lower match
score instead of a wrong result set.

*There is no documented pagination, so there is no pagination.* `limit` caps a single response
at ten; nothing in the contract describes a page or a cursor. Asking for "page 2" by re-sending
the identical request would return the identical adverts and spend another of the fifty daily
calls to do it, so page 2 and beyond return empty without touching the network at all. The
client reads that as the end of the results, which is the truth, and no "next page" control is
manufactured out of a parameter that does not exist.

*Fifty calls a day means the retry is not worth it.* Every other adapter here retries once on a
5xx, which is right when the budget is thousands of calls. At fifty, one slow response costing
two of them is a bad trade -- four percent of the day's capacity for a request that had already
failed once. So this adapter makes exactly one HTTP request per search, which also makes the
budget accounting in app/services/job_hunt/provider_budget.py exact rather than approximate:
one reserved slot is one request.

*The key is a secret with one job.* It is read from settings, sent in a header, and never
written anywhere else. No log line here interpolates it, no exception message contains it, and
nothing it touches is returned to the client -- the API layer maps every JobProviderError to a
fixed sentence (see app/api/job_hunt.py). Until JOBVETTA_API_KEY is configured the provider
raises JobProviderNotConfiguredError, which the product already renders as an honest "Job Hunt
isn't available yet" rather than as an empty job market.

One thing this adapter cannot claim. The owner supplied the *request* contract but not the
*response* schema, so the field names below are the plausible ones and are marked as
unverified. They are all collected in `_FIELD_ALIASES` rather than scattered through the
parser, so correcting them against a real response is a single edit to one table. Any record
that does not yield both a URL and a title is skipped rather than filled in with a placeholder,
which means a wrong guess here produces "no results" and a loud warning in the log, never an
invented job. This is listed in the audit as requiring a live smoke test before release.
"""

import datetime
import logging
from typing import Any, Iterable, Optional

import httpx

from app.services.job_hunt.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    JobProvider,
    JobProviderAuthError,
    JobProviderNotConfiguredError,
    JobProviderRateLimitError,
    JobProviderTimeoutError,
    JobProviderUnavailableError,
    ProviderJob,
    ProviderSearchQuery,
    ProviderSearchResult,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.jobvetta.com/v1"
_SEARCH_PATH = "/jobs"

#: Documented ceiling on `limit`. Not a preference -- values outside 1-10 are outside the
#: contract, so the neutral page_size is clamped into it rather than passed through.
_MIN_LIMIT = 1
_MAX_LIMIT = 10

#: Requests per API key per UTC day, per the supplied contract. Read by the service layer
#: before every outbound call; see app/services/job_hunt/provider_budget.py.
_DAILY_CALL_BUDGET = 50

#: `days` is documented but unbounded in the contract. A year is the widest window the rest of
#: Job Hunt ever asks for (JobSearchRequest caps max_days_old at 365), so anything beyond that
#: could only come from a stored filter written by a future version, and is clamped rather than
#: forwarded.
_MAX_DAYS = 365

#: Response field names, unverified -- see the module docstring. Each entry lists the names
#: this parser will accept for one canonical field, in order of preference. Adding the real
#: name once the live schema is known is a one-line change here and nowhere else.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "job_id", "jobId"),
    "url": ("url", "apply_url", "applyUrl", "job_url", "link"),
    "title": ("title", "job_title", "position"),
    "company": ("company", "company_name", "employer"),
    "location": ("location", "city", "job_location"),
    "description": ("description", "job_description", "summary", "snippet"),
    "posted_at": ("posted_at", "posted_date", "date_posted", "created_at", "date"),
    "employment_type": ("employment_type", "job_type", "contract_type"),
    "work_mode": ("work_mode", "remote_type", "workplace_type"),
    "salary_min": ("salary_min", "min_salary", "salary_from"),
    "salary_max": ("salary_max", "max_salary", "salary_to"),
    "salary_currency": ("salary_currency", "currency", "salary_currency_code"),
    "salary_period": ("salary_period", "salary_type", "salary_interval"),
}

#: Where a list of adverts may live in the envelope. A bare list is accepted too.
_RESULT_CONTAINERS = ("jobs", "results", "data", "items")

#: Where a total-match count may live. Absent means absent: total_available stays None rather
#: than being defaulted to the number of rows in this response, which would make one page of
#: many look like the complete result set.
_TOTAL_CONTAINERS = ("total", "total_results", "totalResults", "count", "hits")

#: Jobvetta's own vocabularies are unverified as well, so these maps are conservative: a value
#: that is not recognised becomes None ("the provider did not tell us") and never a guess.
_WORK_MODES = {
    "remote": "remote",
    "fully_remote": "remote",
    "work_from_home": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "on_site": "onsite",
    "in_office": "onsite",
}
_EMPLOYMENT_TYPES = {
    "full_time": "full_time",
    "fulltime": "full_time",
    "permanent": "full_time",
    "part_time": "part_time",
    "parttime": "part_time",
    "contract": "contract",
    "temporary": "contract",
    "internship": "internship",
    "intern": "internship",
}
_SALARY_PERIODS = {
    "year": "year",
    "yearly": "year",
    "annual": "year",
    "annum": "year",
    "month": "month",
    "monthly": "month",
    "week": "week",
    "weekly": "week",
    "day": "day",
    "daily": "day",
    "hour": "hour",
    "hourly": "hour",
}


class JobvettaJobProvider(JobProvider):
    name = "jobvetta"
    attribution_text = "Jobs by Jobvetta"
    attribution_url = "https://www.jobvetta.com"
    # Six hours, matching the other adapters. Jobvetta publishes no retention condition, so
    # this is a freshness choice rather than a contractual one -- and with fifty calls a day,
    # re-fetching a listing more often than this would spend the budget on adverts that have
    # almost certainly not changed.
    cache_ttl = datetime.timedelta(hours=6)
    daily_call_budget = _DAILY_CALL_BUDGET

    def __init__(
        self,
        api_key: Optional[str],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        base_url: str = _BASE_URL,
    ) -> None:
        # Stripped at construction, and an all-whitespace value becomes None. An env var set
        # to "" or " " is how a key gets "configured" by a deployment template that filled in
        # a blank -- and sending `Bearer ` would turn a missing key into a stream of 401s,
        # which the product would then report as "job search is temporarily unavailable"
        # rather than the truthful "Job Hunt isn't set up yet".
        self._api_key = (api_key or "").strip() or None
        self._timeout = timeout_seconds
        self._base_url = base_url.rstrip("/")

    # -- Search ------------------------------------------------------------------------------

    def search(self, query: ProviderSearchQuery) -> ProviderSearchResult:
        self._require_key()

        if query.page > 1:
            # Not an error and not an empty job market -- there simply is no second page in
            # this contract. Returning empty here rather than raising keeps "load more" a
            # no-op instead of an error banner, and costs nothing from the daily budget.
            logger.debug(
                "Jobvetta has no documented pagination; page %d returned empty without a call",
                query.page,
            )
            return ProviderSearchResult(jobs=[], total_available=None)

        params: dict[str, Any] = {"limit": max(_MIN_LIMIT, min(query.page_size, _MAX_LIMIT))}
        if query.keywords:
            params["q"] = query.keywords
        if query.location:
            params["location"] = query.location
        if query.max_days_old is not None:
            params["days"] = max(1, min(query.max_days_old, _MAX_DAYS))
        # work mode, employment type, experience level and salary floor are deliberately not
        # sent: Jobvetta documents no parameter for any of them. See the module docstring.

        response = self._get(_SEARCH_PATH, params=params)
        payload = self._decode(response)
        raw_jobs = _extract_list(payload)
        if raw_jobs is None:
            raise JobProviderUnavailableError("Jobvetta returned no recognisable job list")

        jobs = []
        for raw in raw_jobs:
            parsed = self._parse_job(raw)
            if parsed is not None:
                jobs.append(parsed)
        if len(jobs) != len(raw_jobs):
            # Loud, because the most likely cause is _FIELD_ALIASES not matching the live
            # schema, and the visible symptom of that would otherwise be a quiet empty screen.
            logger.warning(
                "Jobvetta: skipped %d of %d results that could not be parsed",
                len(raw_jobs) - len(jobs),
                len(raw_jobs),
            )

        return ProviderSearchResult(jobs=jobs, total_available=_extract_total(payload))

    # -- Details -----------------------------------------------------------------------------

    def fetch_job_details(self, provider_job_id: str) -> Optional[ProviderJob]:
        """One advert, in full.

        Implemented because it is part of the provider's contract, and deliberately not wired
        into the search path. Each call spends one of fifty daily requests, so fetching details
        for every result would exhaust the whole day's budget on a single page of ten -- five
        times over. Nothing in the product currently needs a field that only this endpoint
        supplies, and adding a caller is an owner decision with a visible cost, not a detail.
        """
        self._require_key()
        if not provider_job_id or not provider_job_id.strip():
            return None
        response = self._get(f"{_SEARCH_PATH}/{provider_job_id.strip()}", allow_404=True)
        if response.status_code == 404:
            # The advert is gone -- an ordinary outcome for a job board, not a provider fault.
            return None
        payload = self._decode(response)
        if not isinstance(payload, dict):
            raise JobProviderUnavailableError("Jobvetta returned an unexpected payload shape")
        # Some envelopes wrap the object; accept either without demanding one.
        for container in _RESULT_CONTAINERS:
            nested = payload.get(container)
            if isinstance(nested, dict):
                payload = nested
                break
        return self._parse_job(payload)

    # -- Transport ---------------------------------------------------------------------------

    def _require_key(self) -> None:
        if not self._api_key:
            # Distinct from an outage on purpose: this is a deployment gap the owner can
            # close, and it must never degrade into "no jobs found", which would be a claim
            # about the labour market made on no evidence.
            raise JobProviderNotConfiguredError(
                "Jobvetta is selected as JOB_PROVIDER but JOBVETTA_API_KEY is unset"
            )

    def _get(
        self, path: str, params: Optional[dict] = None, *, allow_404: bool = False
    ) -> httpx.Response:
        """One request. No retry -- see the module docstring on the fifty-a-day budget.

        The key travels in the Authorization header and appears nowhere else: not in the URL
        (which lands in access logs and proxy logs), not in a log line here, and not in any of
        the exception messages raised below.
        """
        try:
            response = httpx.get(
                f"{self._base_url}{path}",
                params=params or {},
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise JobProviderTimeoutError(
                f"Jobvetta did not respond within {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise JobProviderUnavailableError(
                f"Could not reach Jobvetta: {type(exc).__name__}"
            ) from exc

        self._raise_for_status(response, allow_404=allow_404)
        return response

    def _raise_for_status(self, response: httpx.Response, *, allow_404: bool = False) -> None:
        if response.status_code in (401, 403):
            raise JobProviderAuthError(
                f"Jobvetta rejected the configured API key ({response.status_code})"
            )
        if response.status_code == 429:
            # Retry-After is echoed into the log so an operator can see how long the key is
            # locked out for. It is not surfaced to the student: the client's message for a
            # 429 is already "try again in a few minutes", and a provider-supplied number in
            # a mobile UI would be one more thing to be wrong.
            retry_after = response.headers.get("Retry-After")
            logger.warning(
                "Jobvetta rate limit hit (429)%s",
                f"; Retry-After: {retry_after}" if retry_after else "",
            )
            raise JobProviderRateLimitError("Jobvetta rate limit exhausted (429)")
        if response.status_code == 404 and allow_404:
            # "That advert no longer exists" is a fact about one job, not a provider failure,
            # so the details path handles it as a normal answer rather than an outage.
            return
        if response.status_code >= 400:
            raise JobProviderUnavailableError(f"Jobvetta returned HTTP {response.status_code}")

    def _decode(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise JobProviderUnavailableError("Jobvetta returned a non-JSON body") from exc

    # -- Mapping -----------------------------------------------------------------------------

    def _parse_job(self, raw: Any) -> Optional[ProviderJob]:
        """One advert, or None.

        None means "this record could not be understood", and the caller drops it. Every field
        below is either a value the provider actually supplied or None -- there is no branch
        that fills a gap with a default, a guess derived from another field, or a placeholder.
        A job with no salary is a job whose salary we do not know, and it travels that way all
        the way to the screen.
        """
        if not isinstance(raw, dict):
            return None

        url = _string(raw, "url")
        title = _string(raw, "title")
        if not url or not title:
            # Both are load-bearing: without a URL the student cannot apply, and without a
            # title there is nothing to show or to match against.
            return None

        provider_job_id = _string(raw, "id") or url
        salary_min = _as_int(_value(raw, "salary_min"))
        salary_max = _as_int(_value(raw, "salary_max"))

        return ProviderJob(
            provider=self.name,
            provider_job_id=provider_job_id,
            url=url,
            title=title,
            company=_string(raw, "company"),
            location=_string(raw, "location"),
            work_mode=_mapped(_string(raw, "work_mode"), _WORK_MODES),
            employment_type=_mapped(_string(raw, "employment_type"), _EMPLOYMENT_TYPES),
            # Jobvetta's contract documents no seniority field, and inferring one from the
            # title would be a guess presented to the student as provider data.
            seniority=None,
            salary_min=salary_min,
            salary_max=salary_max,
            # A currency is only meaningful next to a figure. Attaching one to an advert that
            # quoted no salary would imply we know something about the pay.
            salary_currency=(
                _string(raw, "salary_currency")
                if (salary_min is not None or salary_max is not None)
                else None
            ),
            salary_period=(
                _mapped(_string(raw, "salary_period"), _SALARY_PERIODS)
                if (salary_min is not None or salary_max is not None)
                else None
            ),
            # Nothing in the contract describes a modelled or estimated salary, so any figure
            # here is one the advert itself quoted.
            salary_is_predicted=False,
            description=_string(raw, "description"),
            posted_at=_parse_datetime(_value(raw, "posted_at")),
        )


# -- Envelope and field helpers -----------------------------------------------------------


def _value(raw: dict, canonical: str) -> Any:
    """The first alias of `canonical` that the record actually carries."""
    for alias in _FIELD_ALIASES[canonical]:
        if alias in raw and raw[alias] is not None:
            return raw[alias]
    return None


def _string(raw: dict, canonical: str) -> Optional[str]:
    value = _value(raw, canonical)
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _mapped(value: Optional[str], vocabulary: dict) -> Optional[str]:
    """Translate a provider label into this app's vocabulary, or admit we do not know.

    Unrecognised stays None rather than falling back to the most common value. "onsite" is the
    most common work mode, and defaulting to it would tell a student a remote job requires
    relocation -- a fabricated fact about a real advert.
    """
    if value is None:
        return None
    return vocabulary.get(value.strip().lower().replace("-", "_").replace(" ", "_"))


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _extract_list(payload: Any) -> Optional[list]:
    """The adverts, whether the response is a bare list or an envelope around one."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for container in _RESULT_CONTAINERS:
            value = payload.get(container)
            if isinstance(value, list):
                return value
    return None


def _extract_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for container in _TOTAL_CONTAINERS:
        total = _as_int(payload.get(container))
        if total is not None and total >= 0:
            return total
    return None


_DATE_FORMATS: Iterable[str] = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_datetime(value: Any) -> Optional[datetime.datetime]:
    """A posting date, naive UTC, or None.

    Naive because every other datetime in this schema is, and mixing the two produces
    comparison errors at runtime rather than at import. An unparseable date becomes None: a
    listing whose age we cannot establish is treated as "we do not know when this was posted",
    which the ranking code already handles, rather than as posted right now.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A Unix timestamp. Guarded against nonsense values that would raise from utcfromtimestamp.
        try:
            return datetime.datetime.utcfromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None
    if parsed.tzinfo is None:
        return parsed
    # Converted, not stripped. Dropping the offset would keep the wall-clock reading of some
    # other timezone and silently shift the advert's age by up to a day.
    return parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
