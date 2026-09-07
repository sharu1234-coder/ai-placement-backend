"""The provider-agnostic contract every job source must satisfy.

Why an abstraction rather than calling one API directly (spec section 7): the provider
decision is genuinely open -- it depends on commercial terms the owner has to approve -- and
the terms differ enough between candidates that they change behaviour, not just the URL.
Careerjet requires the end user's IP and user agent on every call; Adzuna forbids commercial
use beyond a 14-day evaluation without written consent and demands a "Jobs by Adzuna"
attribution link. Encoding either of those into the search service would make swapping
providers a rewrite instead of a config change, and would put a legal obligation somewhere
nobody looks.

So each adapter owns its own credentials, quirks, attribution string and cache-retention
rule, and hands back the same `ProviderJob` shape. app/services/job_hunt/service.py never
imports a concrete provider -- it takes one through the get_job_provider() dependency, exactly
the way StorageProvider and EmailProvider already work in this codebase.
"""

import dataclasses
import datetime
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class JobProviderError(Exception):
    """Base for every provider failure. Callers translate these into HTTP responses without
    needing to know which provider was in use -- and, critically, without ever putting the
    provider's own message in front of a student (see app/api/job_hunt.py)."""


class JobProviderNotConfiguredError(JobProviderError):
    """No provider credentials are set. Distinct from an outage: this is a deployment state
    the owner can fix, and it must never degrade into "no jobs found", which would tell the
    student a falsehood about the labour market."""


class JobProviderAuthError(JobProviderError):
    """Credentials were rejected (401/403). Operationally urgent -- surfaced as a distinct log
    signal so a revoked key is not mistaken for a quiet day in the job market."""


class JobProviderRateLimitError(JobProviderError):
    """The provider's own quota is exhausted (429, or a documented quota response)."""


class JobProviderTimeoutError(JobProviderError):
    """The provider did not answer in time."""


class JobProviderUnavailableError(JobProviderError):
    """Any other provider-side failure: 5xx, connection reset, malformed payload."""


@dataclasses.dataclass(frozen=True)
class ProviderJob:
    """One advert in this app's own vocabulary.

    Every optional field defaults to None and means "the provider did not tell us". Adapters
    must never invent a value to fill a gap -- an unknown work mode stays None and becomes an
    honest "unknown factor" on the match explanation, rather than being guessed at from the
    job title and then shown to the student as fact.
    """

    provider: str
    provider_job_id: str
    url: str
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[str] = None
    employment_type: Optional[str] = None
    seniority: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: Optional[str] = None
    salary_period: Optional[str] = None
    salary_is_predicted: bool = False
    description: Optional[str] = None
    posted_at: Optional[datetime.datetime] = None


@dataclasses.dataclass(frozen=True)
class ProviderSearchQuery:
    """What the service asks for, in provider-neutral terms. Each adapter maps these onto its
    own parameter names and drops what it cannot express, rather than the service having to
    know which provider supports which filter."""

    keywords: Optional[str] = None
    location: Optional[str] = None
    remote_only: bool = False
    employment_type: Optional[str] = None
    experience_level: Optional[str] = None
    salary_min: Optional[int] = None
    max_days_old: Optional[int] = None
    page: int = 1
    page_size: int = 20
    # Careerjet contractually requires the end user's IP and user agent on every query. Passed
    # through here rather than read from a global, so it is visible at the call site that this
    # data leaves the building, and so a provider that does not need it simply ignores it.
    end_user_ip: Optional[str] = None
    end_user_agent: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ProviderSearchResult:
    jobs: list[ProviderJob]
    # None when the provider does not report a total. Never defaulted to len(jobs), which
    # would make page 1 of many look like the complete result set.
    total_available: Optional[int] = None


class JobProvider(ABC):
    """Implemented once per job source."""

    #: Stable key stored on every JobListing row, and the value of JOB_PROVIDER in settings.
    name: str = "base"

    #: Shown beside every listing this provider supplied. Several providers make this a
    #: contractual condition of use, so it is part of the interface rather than a UI choice.
    attribution_text: str = ""
    attribution_url: Optional[str] = None

    #: How long a fetched listing may still be shown before it must be refetched. Expressed
    #: per provider because retention is a terms question, not a performance one.
    cache_ttl: datetime.timedelta = datetime.timedelta(hours=6)

    @abstractmethod
    def search(self, query: ProviderSearchQuery) -> ProviderSearchResult:
        """Return real adverts, or raise a JobProviderError subclass.

        Must never return fabricated listings, and must never swallow a failure into an empty
        list -- "no results" and "the provider is down" are different facts and the student is
        told which one happened (spec section 22).
        """


# -- Transport ------------------------------------------------------------------------------
# One retry, never more (spec phase 15: "Use bounded retries/timeouts. Avoid retry storms").
#
# The retry is deliberately narrow. It fires only on a transport failure or a 5xx -- things
# that plausibly differ on a second attempt -- and never on 4xx: retrying a 401 cannot fix
# revoked credentials, and retrying a 429 is how a rate limit becomes a ban. Nor does it wrap
# response parsing; a body that is not JSON will not become JSON on a second read, and
# retrying it would just double the latency of a provider outage.
#
# The per-attempt timeout is 10s rather than 15s so that two attempts plus the backoff stay
# inside a ~21s worst case. A 15s default with a retry would have pushed a failing search past
# 30s, which is long enough to hit a reverse proxy's own timeout and turn a clean 504 from us
# into an opaque one from the proxy.
_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_TIMEOUT_SECONDS = 10.0


def get_with_one_retry(
    url: str,
    *,
    params: dict,
    timeout: float,
    provider_name: str,
) -> httpx.Response:
    """GET with a single bounded retry on transient failure.

    Raises JobProviderTimeoutError or JobProviderUnavailableError -- the same classes the
    adapters already raise -- so callers keep mapping errors exactly as before. Returns the
    response untouched; status-code semantics stay in the adapter that knows the provider.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        is_final = attempt == _MAX_ATTEMPTS - 1
        try:
            response = httpx.get(url, params=params, timeout=timeout)
        except httpx.TimeoutException as exc:
            last_exc = JobProviderTimeoutError(
                f"{provider_name} did not respond within {timeout}s"
            )
            last_exc.__cause__ = exc
        except httpx.HTTPError as exc:
            last_exc = JobProviderUnavailableError(
                f"Could not reach {provider_name}: {type(exc).__name__}"
            )
            last_exc.__cause__ = exc
        else:
            # A 5xx is the provider telling us it is having a bad moment; a 4xx is it telling
            # us something about our request, which a second identical request cannot change.
            if response.status_code < 500 or is_final:
                return response
            logger.warning(
                "%s returned HTTP %d; retrying once", provider_name, response.status_code
            )
            time.sleep(_RETRY_BACKOFF_SECONDS)
            continue

        if is_final:
            raise last_exc
        logger.warning("%s request failed (%s); retrying once", provider_name, type(last_exc).__name__)
        time.sleep(_RETRY_BACKOFF_SECONDS)

    # Unreachable: the final attempt either returns or raises above. Present so a future edit
    # to the loop cannot silently fall through to None.
    raise last_exc or JobProviderUnavailableError(f"{provider_name} could not be reached")
