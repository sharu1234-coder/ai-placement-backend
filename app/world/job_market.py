"""The one real World source: job adverts, through the existing `JobProvider`.

Reuses, rather than reimplements: `JobProvider` and its typed errors, `dedupe.deduplicate`,
`provider_budget.reserve_call`, and `normalization`'s skill extraction and seniority
inference. A second job-search architecture is exactly what the brief forbids and would be a
second set of provider quirks to keep in step.

It deliberately does **not** reuse `app/services/job_hunt/service.py::search`. That function
creates a search profile, upserts `JobListing` and `JobListingMatch` rows and scores every
advert against the student's resume. All three are right for the authenticated job-search
feature and wrong here: world retrieval must not persist external adverts (no new retention
surface) and must not send or join personal data. So this module calls the provider directly
and keeps nothing.

Every failure is translated into a `WorldError` subclass. The provider's own message never
travels outward -- Adzuna's or Careerjet's wording is not something a student should read, and
a quota message in particular must not be mistaken for a quiet job market.
"""

from __future__ import annotations

import datetime
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.services.job_hunt import dedupe, normalization, provider_budget
from app.services.job_hunt.providers.base import (
    JobProvider,
    JobProviderAuthError,
    JobProviderError,
    JobProviderNotConfiguredError,
    JobProviderRateLimitError,
    JobProviderTimeoutError,
    JobProviderUnavailableError,
    ProviderJob,
    ProviderSearchQuery,
)
from app.world.cache import TtlCache, query_key
from app.world.capabilities import Capability, require_supported
from app.world.contracts import (
    CompanyHiring,
    DataCategory,
    EvidenceState,
    GeographicComparison,
    JobOpening,
    Observation,
    SampleScope,
    SkillDemand,
    SourceKind,
    SourceNotConfigured,
    SourceQuotaExhausted,
    SourceRef,
    SourceRejectedCredentials,
    SourceUnavailable,
    Unknown,
    UntrustedText,
)

logger = logging.getLogger(__name__)

#: Hard ceiling on adverts retrieved for one world query, independent of what a caller asks
#: for. Bounds the provider cost, the response size and the work done per request.
MAX_RESULTS = 25

#: Below this many unique adverts, an aggregate figure is reported as INSUFFICIENT rather than
#: as a percentage. Twenty adverts is already a thin basis for a market claim; five is noise,
#: and "1 of 3 mentioned Kubernetes" presented as 33% is the kind of number that gets repeated.
MIN_SAMPLE_FOR_AGGREGATE = 8

#: Locations compared in one call. Each costs a provider call against the same daily budget.
MAX_COMPARISON_LOCATIONS = 3

#: Shared across requests within one worker. See `app/world/cache.py` for why this is not the
#: quota mechanism.
_cache: TtlCache[list[ProviderJob]] = TtlCache()


def reset_cache() -> None:
    """For tests, and for an operator who has changed provider configuration."""
    _cache.clear()


@dataclass(frozen=True)
class WorldQuery:
    """What a world retrieval is allowed to carry.

    The whole list is role, geography, experience level and approved filters. There is no
    field for a resume, a profile, a skill inventory or an identifier -- not because callers
    are trusted to leave them out, but because there is nowhere to put them. A provider's
    terms and logging behaviour are outside this project's control, so what is sent has to be
    the minimum that makes the query mean what it says.
    """

    keywords: Optional[str] = None
    location: Optional[str] = None
    experience_level: Optional[str] = None
    employment_type: Optional[str] = None
    remote_only: bool = False
    max_days_old: Optional[int] = None
    limit: int = 20

    def describe(self) -> str:
        parts = [self.keywords or "any role"]
        if self.location:
            parts.append(f"in {self.location}")
        if self.experience_level:
            parts.append(f"at {self.experience_level} level")
        if self.employment_type:
            parts.append(self.employment_type)
        if self.remote_only:
            parts.append("remote only")
        if self.max_days_old:
            parts.append(f"posted within {self.max_days_old} days")
        return " ".join(parts)

    def to_provider_query(self) -> ProviderSearchQuery:
        return ProviderSearchQuery(
            keywords=self.keywords,
            location=self.location,
            remote_only=self.remote_only,
            employment_type=self.employment_type,
            experience_level=normalization.normalize_experience_level(self.experience_level),
            max_days_old=self.max_days_old,
            page=1,
            page_size=min(max(self.limit, 1), MAX_RESULTS),
            # Careerjet contractually wants the end user's IP and user agent. Not supplied
            # here: a world query is not made on behalf of a browsing user session, and
            # inventing an address to satisfy a contractual field would be worse than
            # declining to use a provider that requires it. The adapter reports the omission
            # as its own limitation.
        )


# --------------------------------------------------------------------------- retrieval


def _source_ref(provider: JobProvider, job: Optional[ProviderJob] = None) -> SourceRef:
    return SourceRef(
        provider=provider.name,
        kind=SourceKind.LIVE_PROVIDER,
        provider_record_id=job.provider_job_id if job else None,
        url=normalization.safe_external_url(job.url) if job else None,
        attribution_text=provider.attribution_text or None,
        attribution_url=provider.attribution_url,
    )


def _derived_ref(provider: JobProvider) -> SourceRef:
    return SourceRef(
        provider=provider.name,
        kind=SourceKind.DERIVED,
        attribution_text=provider.attribution_text or None,
        attribution_url=provider.attribution_url,
    )


def _translate(exc: JobProviderError, provider_name: str) -> Exception:
    """Provider failure to world failure, preserving the distinction that matters.

    "Not configured", "quota spent", "credentials refused" and "it broke" need different
    responses from an operator and different words to a student. Collapsing them -- which a
    single `except JobProviderError` would do -- is how a revoked key gets read as a quiet day
    in the job market.
    """
    if isinstance(exc, JobProviderNotConfiguredError):
        return SourceNotConfigured(
            "No job-data source is configured for this deployment, so live openings cannot be retrieved."
        )
    if isinstance(exc, JobProviderRateLimitError):
        return SourceQuotaExhausted(
            f"The daily request budget for the {provider_name} job source is exhausted."
        )
    if isinstance(exc, JobProviderAuthError):
        return SourceRejectedCredentials(f"The {provider_name} job source refused this deployment's credentials.")
    if isinstance(exc, JobProviderTimeoutError):
        return SourceUnavailable(f"The {provider_name} job source did not respond in time.")
    if isinstance(exc, JobProviderUnavailableError):
        return SourceUnavailable(f"The {provider_name} job source could not be reached.")
    return SourceUnavailable(f"The {provider_name} job source failed.")


def _retrieve(
    db: Session,
    provider: JobProvider,
    query: WorldQuery,
    *,
    now: Optional[datetime.datetime] = None,
) -> tuple[list[ProviderJob], SampleScope, bool]:
    """Fetch, deduplicate and scope one sample. Returns (unique jobs, scope, from_cache).

    The budget is reserved before the call and only on a cache miss: a cached answer costs the
    provider nothing, so metering it would spend a slot that was never used.
    """
    key = query_key(
        provider.name, query.keywords, query.location, query.experience_level,
        query.employment_type, query.remote_only, query.max_days_old, query.limit,
    )
    cached = _cache.get(key, now=now)
    if cached is not None:
        unique = cached
        scope = SampleScope(
            query_description=query.describe(),
            providers=(provider.name,),
            retrieved_count=len(unique),
            unique_count=len(unique),
            page_size=min(max(query.limit, 1), MAX_RESULTS),
        )
        return list(unique), scope, True

    try:
        # Translated like any other provider failure. `reserve_call` raises the provider
        # layer's own `JobProviderRateLimitError`, and letting that escape untranslated would
        # put a `JobProviderError` in front of a caller that only handles `WorldError` -- the
        # quota case would then surface as an unhandled exception rather than as the one
        # outcome an operator most needs to recognise.
        provider_budget.reserve_call(db, provider.name, getattr(provider, "daily_call_budget", None))
    except JobProviderError as exc:
        logger.info("world job retrieval blocked: provider=%s error=%s", provider.name, type(exc).__name__)
        raise _translate(exc, provider.name) from exc

    try:
        result = provider.search(query.to_provider_query())
    except JobProviderError as exc:
        # Logged with the provider and the error class only. A provider's message can carry
        # the request URL, and the request URL carries the API key for several of these
        # providers -- so the message itself never reaches the log.
        logger.warning("world job retrieval failed: provider=%s error=%s", provider.name, type(exc).__name__)
        raise _translate(exc, provider.name) from exc

    unique = dedupe.deduplicate(result.jobs)[:MAX_RESULTS]
    scope = SampleScope(
        query_description=query.describe(),
        providers=(provider.name,),
        retrieved_count=len(result.jobs),
        unique_count=len(unique),
        total_available=result.total_available,
        page_size=min(max(query.limit, 1), MAX_RESULTS),
    )
    _cache.put(key, list(unique), provider.cache_ttl, now=now)
    return unique, scope, False


def _to_opening(job: ProviderJob) -> tuple[JobOpening, EvidenceState, tuple[str, ...]]:
    """One provider advert to a world observation value.

    Returns the evidence state alongside, because what is missing decides it: an advert with no
    posting date and no company cannot support a freshness judgement or a company claim, and
    calling that CONFIRMED would overstate it.
    """
    skills = tuple(normalization.extract_job_skills(job.title, job.description))
    # Only inferred when the provider did not say. Inference from a title is weaker evidence
    # than the provider's own field, and overwriting the latter would discard the better one.
    seniority = job.seniority or normalization.seniority_from_title(job.title)

    limitations: list[str] = []
    evidence = EvidenceState.CONFIRMED
    if job.posted_at is None:
        limitations.append("the source did not report a posting date, so freshness is unknown")
        evidence = EvidenceState.PARTIAL
    if not job.company:
        limitations.append("the source did not name the company")
        evidence = EvidenceState.PARTIAL
    if job.seniority is None and seniority is not None:
        limitations.append("experience level was inferred from the job title, not stated by the source")
        evidence = EvidenceState.PARTIAL
    if not skills:
        limitations.append("no recognised skills were found in the advert text")

    opening = JobOpening(
        title=job.title,
        company=job.company,
        location=job.location,
        employment_type=job.employment_type,
        experience_level=normalization.normalize_experience_level(seniority),
        work_mode=job.work_mode or normalization.work_mode_from_text(job.title, job.description),
        skills=skills,
        url=normalization.safe_external_url(job.url),
        description=None,
    )
    return opening, evidence, tuple(limitations)


def find_openings(
    db: Session,
    provider: JobProvider,
    query: WorldQuery,
    *,
    fresher_only: bool = False,
    include_descriptions: bool = False,
    now: Optional[datetime.datetime] = None,
) -> list[Observation[JobOpening]]:
    """Live openings matching `query`, each as its own observation.

    A list of observations rather than one observation of a list: each advert has its own
    posting date, its own completeness and therefore its own freshness and evidence state.
    Flattening them would force one label onto twenty adverts of differing age.
    """
    capability = require_supported(Capability.FRESHER_OPENINGS if fresher_only else Capability.JOB_OPENINGS)
    effective = query
    if fresher_only and not query.experience_level:
        effective = WorldQuery(**{**query.__dict__, "experience_level": "fresher"})

    unique, _scope, from_cache = _retrieve(db, provider, effective, now=now)
    retrieved_at = now or _utcnow()

    observations: list[Observation[JobOpening]] = []
    for job in unique:
        opening, evidence, limitations = _to_opening(job)
        if fresher_only and opening.experience_level not in (None, "fresher", "junior"):
            # The provider either ignored the filter or disagrees with the title. Dropped
            # rather than relabelled: showing a senior advert under "fresher roles" is the
            # error that costs a student an application.
            continue
        if include_descriptions and job.description:
            opening = JobOpening(
                **{
                    **opening.__dict__,
                    "description": UntrustedText(job.description, _source_ref(provider, job)),
                }
            )
        all_limitations = capability.limitations + limitations
        if from_cache:
            all_limitations += ("served from this worker's cache rather than a fresh provider call",)
        observations.append(
            Observation(
                value=opening,
                category=DataCategory.JOB_OPENING,
                source=_source_ref(provider, job),
                retrieved_at=retrieved_at,
                published_at=job.posted_at,
                location=effective.location,
                evidence=evidence,
                limitations=all_limitations,
            )
        )
    return observations


# --------------------------------------------------------------------------- derived views


def skill_demand(
    db: Session,
    provider: JobProvider,
    query: WorldQuery,
    *,
    top_n: int = 10,
    now: Optional[datetime.datetime] = None,
) -> Observation[tuple[SkillDemand, ...]] | Unknown:
    """How often each skill appears in one retrieved sample.

    Returns `Unknown(INSUFFICIENT)` below `MIN_SAMPLE_FOR_AGGREGATE`. That threshold is the
    whole honesty of this function: a percentage computed over three adverts reads exactly like
    one computed over three hundred, and the reader cannot tell which they were given.
    """
    capability = require_supported(Capability.SKILL_DEMAND)
    unique, scope, _cached = _retrieve(db, provider, query, now=now)

    if scope.unique_count < MIN_SAMPLE_FOR_AGGREGATE:
        return Unknown(
            category=DataCategory.SKILL_DEMAND,
            evidence=EvidenceState.INSUFFICIENT,
            reason=(
                f"only {scope.unique_count} unique advert(s) were retrieved for this query; "
                f"at least {MIN_SAMPLE_FOR_AGGREGATE} are needed before a frequency is meaningful"
            ),
            remediation="Broaden the role or geography, or ask about individual openings instead.",
            attempted_sources=(provider.name,),
        )

    counter: Counter[str] = Counter()
    for job in unique:
        # set() per advert: an advert that says "Python" four times is one advert mentioning
        # Python, not four. Counting mentions would let one verbose employer set the trend.
        counter.update(set(normalization.extract_job_skills(job.title, job.description)))

    demands = tuple(
        SkillDemand(skill=skill, openings_mentioning=count, sample_size=scope.unique_count)
        for skill, count in counter.most_common(top_n)
    )
    return Observation(
        value=demands,
        category=DataCategory.SKILL_DEMAND,
        source=_derived_ref(provider),
        retrieved_at=now or _utcnow(),
        published_at=_oldest_posting(unique),
        location=query.location,
        evidence=EvidenceState.CONFIRMED if demands else EvidenceState.INSUFFICIENT,
        limitations=capability.limitations,
        scope=scope,
    )


def company_hiring(
    db: Session,
    provider: JobProvider,
    query: WorldQuery,
    *,
    top_n: int = 10,
    now: Optional[datetime.datetime] = None,
) -> Observation[tuple[CompanyHiring, ...]] | Unknown:
    """Which companies appear in one sample, and with how many adverts."""
    capability = require_supported(Capability.COMPANY_HIRING)
    unique, scope, _cached = _retrieve(db, provider, query, now=now)

    named = [job for job in unique if job.company]
    if len(named) < MIN_SAMPLE_FOR_AGGREGATE:
        return Unknown(
            category=DataCategory.COMPANY_HIRING,
            evidence=EvidenceState.INSUFFICIENT,
            reason=(
                f"only {len(named)} of {scope.unique_count} retrieved advert(s) named a company; "
                "that is too few to say anything about who is hiring"
            ),
            remediation="Broaden the query, or ask about individual openings instead.",
            attempted_sources=(provider.name,),
        )

    by_company: dict[str, list[str]] = {}
    for job in named:
        by_company.setdefault(job.company.strip(), []).append(job.title)

    ranked = sorted(by_company.items(), key=lambda item: (-len(item[1]), item[0]))[:top_n]
    rows = tuple(
        CompanyHiring(
            company=company,
            openings_in_sample=len(titles),
            sample_size=scope.unique_count,
            titles=tuple(titles[:5]),
        )
        for company, titles in ranked
    )
    return Observation(
        value=rows,
        category=DataCategory.COMPANY_HIRING,
        source=_derived_ref(provider),
        retrieved_at=now or _utcnow(),
        published_at=_oldest_posting(named),
        location=query.location,
        evidence=EvidenceState.CONFIRMED,
        limitations=capability.limitations,
        scope=scope,
    )


def role_requirements(
    db: Session,
    provider: JobProvider,
    query: WorldQuery,
    *,
    top_n: int = 15,
    now: Optional[datetime.datetime] = None,
) -> Observation[tuple[SkillDemand, ...]] | Unknown:
    """What adverts for this role ask for.

    The same computation as `skill_demand` under a different category, and that is the point
    rather than an oversight: this layer has one kind of evidence about role requirements --
    what employers wrote in adverts -- and presenting it under two categories as if they were
    two independent sources would be manufacturing corroboration.
    """
    capability = require_supported(Capability.ROLE_REQUIREMENTS)
    demand = skill_demand(db, provider, query, top_n=top_n, now=now)
    if isinstance(demand, Unknown):
        return Unknown(
            category=DataCategory.ROLE_REQUIREMENTS,
            evidence=demand.evidence,
            reason=demand.reason,
            remediation=demand.remediation,
            attempted_sources=demand.attempted_sources,
        )
    return Observation(
        value=demand.value,
        category=DataCategory.ROLE_REQUIREMENTS,
        source=demand.source,
        retrieved_at=demand.retrieved_at,
        published_at=demand.published_at,
        location=demand.location,
        evidence=demand.evidence,
        limitations=capability.limitations,
        scope=demand.scope,
    )


def compare_locations(
    db: Session,
    provider: JobProvider,
    query: WorldQuery,
    locations: Sequence[str],
    *,
    now: Optional[datetime.datetime] = None,
) -> Observation[GeographicComparison] | Unknown:
    """One retrieval per location, compared, with every sample size retained.

    A partial outcome is honest here: if one location's retrieval fails, the ones that
    succeeded are still returned, with the failure recorded as a limitation. What must not
    happen is a silent substitution -- a failed Pune query answered with Bangalore data -- so
    a location that failed is absent from the result rather than approximated.
    """
    capability = require_supported(Capability.GEOGRAPHIC_COMPARISON)
    wanted = [loc.strip() for loc in locations if loc and loc.strip()][:MAX_COMPARISON_LOCATIONS]
    if len(wanted) < 2:
        return Unknown(
            category=DataCategory.GEOGRAPHIC_COMPARISON,
            evidence=EvidenceState.INSUFFICIENT,
            reason="a comparison needs at least two locations",
            remediation="Supply two or more locations.",
            attempted_sources=(provider.name,),
        )

    counts: dict[str, int] = {}
    sizes: dict[str, int] = {}
    oldest: Optional[datetime.datetime] = None
    failures: list[str] = []
    first_error: Optional[Exception] = None

    for location in wanted:
        scoped = WorldQuery(**{**query.__dict__, "location": location})
        try:
            unique, scope, _cached = _retrieve(db, provider, scoped, now=now)
        except (SourceUnavailable, SourceQuotaExhausted) as exc:
            # One provider hiccup, or a budget that ran out partway, must not take the whole
            # comparison down when other locations did return.
            failures.append(f"{location}: {exc.code}")
            first_error = first_error or exc
            continue
        counts[location] = scope.unique_count
        sizes[location] = scope.unique_count
        candidate = _oldest_posting(unique)
        if candidate and (oldest is None or candidate < oldest):
            oldest = candidate

    if not counts:
        # Everything failed, so there is no partial answer to give. The first error is raised
        # rather than swallowed into an Unknown: "not configured" and "quota spent" are
        # operator-actionable facts.
        if first_error is not None:
            raise first_error
        return Unknown(
            category=DataCategory.GEOGRAPHIC_COMPARISON,
            evidence=EvidenceState.INSUFFICIENT,
            reason="no location returned any adverts",
            attempted_sources=(provider.name,),
        )

    scope = SampleScope(
        query_description=f"{query.describe()} across {', '.join(counts)}",
        providers=(provider.name,),
        retrieved_count=sum(sizes.values()),
        unique_count=sum(sizes.values()),
        page_size=min(max(query.limit, 1), MAX_RESULTS),
    )
    limitations = capability.limitations
    if failures:
        limitations += (f"no data retrieved for: {'; '.join(failures)}",)
    thin = [loc for loc, size in sizes.items() if size < MIN_SAMPLE_FOR_AGGREGATE]
    if thin:
        limitations += (
            f"sample too small to compare reliably for: {', '.join(sorted(thin))}",
        )
    return Observation(
        value=GeographicComparison(
            skill_or_role=query.keywords or "any role",
            by_location=counts,
            sample_sizes=sizes,
        ),
        category=DataCategory.GEOGRAPHIC_COMPARISON,
        source=_derived_ref(provider),
        retrieved_at=now or _utcnow(),
        published_at=oldest,
        location=", ".join(counts),
        evidence=EvidenceState.INSUFFICIENT if thin else EvidenceState.CONFIRMED,
        limitations=limitations,
        scope=scope,
    )


# --------------------------------------------------------------------------- helpers


def _oldest_posting(jobs: Sequence[ProviderJob]) -> Optional[datetime.datetime]:
    """The oldest posting date in a sample, or None if nothing is dated.

    The oldest rather than the newest, because an aggregate is only as current as its stalest
    input. Taking the newest would let one advert posted this morning make a quarter-old sample
    look fresh.
    """
    dated = [job.posted_at for job in jobs if job.posted_at is not None]
    return min(dated) if dated else None


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
