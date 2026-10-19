"""World Intelligence through the VA-4 Tool Registry. Read-only.

These sit beside the personal read tools and obey the same contract, with two differences that
matter:

* **They reach outside.** A personal read tool queries this deployment's own database; these
  spend a provider call against a daily budget and can fail in ways a database does not --
  unconfigured, quota-exhausted, credentials refused. Each becomes a typed, redacted error
  rather than an empty result, because "no provider configured" and "no jobs found" are
  different facts and the second is a claim about the labour market.
* **They carry provenance.** Every row says which provider it came from, when it was
  retrieved, how fresh it is and what the evidence state is. Any aggregate also carries the
  sample it was computed over, so a count cannot be read as a market statistic.

What they deliberately do not do: send any personal data. A world query carries a role, a
geography, an experience level and approved filters -- the input schemas have no field for
anything else, so a caller cannot attach a resume or a profile even by trying. The join between
public listings and private career evidence already exists for the authenticated job-search
feature in `app/services/job_hunt/matching.py`, and it stays there.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from pydantic import Field

from app.tools.contracts import (
    Idempotency,
    ToolContext,
    ToolError,
    ToolInput,
    ToolKind,
    ToolOutput,
    ToolSpec,
    Verification,
)
from app.world import job_market
from app.world.capabilities import Capability, declaration
from app.world.contracts import (
    CapabilityUnsupported,
    Observation,
    SourceNotConfigured,
    SourceQuotaExhausted,
    SourceRejectedCredentials,
    SourceUnavailable,
    Unknown,
    WorldError,
)

logger = logging.getLogger(__name__)

#: Rows returned by one world tool. Smaller than the personal read tools' ceiling because each
#: row carries provenance, and because a spoken answer listing ten adverts is already long.
MAX_WORLD_ITEMS = 10


class WorldToolError(ToolError):
    """A world retrieval failure, translated for the registry.

    Keeps the world layer's `code` so a caller can still tell "not configured" from "quota
    spent" from "it broke", while putting the message through the registry's own redaction
    rules. The provider's own wording never reaches here.
    """

    def __init__(self, tool: str, cause: WorldError) -> None:
        self.tool = tool
        self.cause = cause
        self.code = cause.code
        super().__init__(f"{tool}: {cause.safe_message()}")

    def safe_message(self) -> str:
        return self.cause.safe_message()


def _guard(tool_name: str):
    """Translate world failures into registry failures.

    A decorator rather than a try/except in each handler: there are six handlers and the
    mapping must be identical in all of them, because the difference between an unconfigured
    source and an exhausted quota is exactly what an operator reads first.
    """

    def wrap(handler):
        def run(context, args):
            try:
                return handler(context, args)
            except (
                SourceNotConfigured,
                SourceUnavailable,
                SourceQuotaExhausted,
                SourceRejectedCredentials,
                CapabilityUnsupported,
            ) as exc:
                logger.info("world tool %s unavailable: code=%s", tool_name, exc.code)
                raise WorldToolError(tool_name, exc) from exc

        run.__name__ = handler.__name__
        return run

    return wrap


# --------------------------------------------------------------------------- shared schemas


class WorldSearchInput(ToolInput):
    """The only things a world query may carry.

    No profile, no resume, no skill list, no identifier -- and `extra="forbid"` from
    `ToolInput` means an attempt to add one is rejected rather than ignored. What a provider
    logs or retains is outside this project's control, so what leaves has to be the minimum
    that makes the query mean what it says.
    """

    role: Optional[str] = Field(default=None, max_length=120)
    location: Optional[str] = Field(default=None, max_length=120)
    experience_level: Optional[str] = Field(default=None, max_length=30)
    employment_type: Optional[str] = Field(default=None, max_length=30)
    remote_only: bool = False
    max_days_old: Optional[int] = Field(default=None, ge=1, le=90)
    limit: int = Field(default=MAX_WORLD_ITEMS, ge=1, le=MAX_WORLD_ITEMS)


class Provenance(ToolOutput):
    """Where one row came from. Attached per row, not per response.

    Per row because a response can mix providers and ages, and a single response-level label
    would have to pick one and be wrong about the rest.
    """

    provider: str
    source_kind: str
    retrieved_at: str
    published_at: Optional[str] = None
    freshness: str
    evidence: str
    url: Optional[str] = None
    attribution: Optional[str] = None
    limitations: list[str] = Field(default_factory=list)


class SampleInfo(ToolOutput):
    """The scope of any aggregate. Required wherever a number is reported."""

    description: str
    providers: list[str] = Field(default_factory=list)
    retrieved_count: int = 0
    unique_count: int = 0
    total_available: Optional[int] = None


class WorldUnavailable(ToolOutput):
    """An explicit "we could not answer", with the reason.

    A result shape rather than only an exception, for the cases where the honest answer is
    "not enough evidence" rather than "the source failed". A consumer that got an empty list
    instead would say "there are no jobs", which is a different and false statement.
    """

    available: bool = False
    evidence: str
    reason: str
    remediation: Optional[str] = None


def _iso(value: Optional[datetime.datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _provenance(observation: Observation) -> Provenance:
    source = observation.source
    return Provenance(
        provider=source.provider,
        source_kind=source.kind.value,
        retrieved_at=observation.retrieved_at.isoformat(),
        published_at=_iso(observation.published_at),
        freshness=observation.freshness().value,
        evidence=observation.evidence.value,
        url=source.url,
        attribution=source.attribution_text,
        limitations=list(observation.limitations)[:5],
    )


def _sample(observation: Observation) -> Optional[SampleInfo]:
    scope = observation.scope
    if scope is None:
        return None
    return SampleInfo(
        description=scope.describe(),
        providers=list(scope.providers),
        retrieved_count=scope.retrieved_count,
        unique_count=scope.unique_count,
        total_available=scope.total_available,
    )


def _query(args: WorldSearchInput) -> job_market.WorldQuery:
    return job_market.WorldQuery(
        keywords=args.role,
        location=args.location,
        experience_level=args.experience_level,
        employment_type=args.employment_type,
        remote_only=args.remote_only,
        max_days_old=args.max_days_old,
        limit=args.limit,
    )


def _provider():
    from app.services.job_hunt.providers.factory import get_job_provider

    return get_job_provider()


def _unknown(found: Unknown) -> WorldUnavailable:
    return WorldUnavailable(
        available=False,
        evidence=found.evidence.value,
        reason=found.reason,
        remediation=found.remediation,
    )


# --------------------------------------------------------------------------- openings


class OpeningRow(ToolOutput):
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    employment_type: Optional[str] = None
    experience_level: Optional[str] = None
    work_mode: Optional[str] = None
    skills: list[str] = Field(default_factory=list)
    provenance: Provenance
    # Deliberately no `description`: the advert's prose is untrusted external text, it is the
    # largest field by far, and no VA-5 consumer needs it. It is available inside the world
    # layer as UntrustedText for an approved future consumer.


class OpeningsResult(ToolOutput):
    available: bool = True
    openings: list[OpeningRow] = Field(default_factory=list)
    truncated: bool = False
    sample: Optional[SampleInfo] = None
    capability_limitations: list[str] = Field(default_factory=list)


def _openings(context: ToolContext, args: WorldSearchInput, *, fresher: bool) -> OpeningsResult:
    provider = _provider()
    observations = job_market.find_openings(
        context.db, provider, _query(args), fresher_only=fresher
    )
    kept = observations[:MAX_WORLD_ITEMS]
    capability = declaration(Capability.FRESHER_OPENINGS if fresher else Capability.JOB_OPENINGS)
    return OpeningsResult(
        available=True,
        openings=[
            OpeningRow(
                title=obs.value.title,
                company=obs.value.company,
                location=obs.value.location,
                employment_type=obs.value.employment_type,
                experience_level=obs.value.experience_level,
                work_mode=obs.value.work_mode,
                skills=list(obs.value.skills)[:10],
                provenance=_provenance(obs),
            )
            for obs in kept
        ],
        truncated=len(observations) > len(kept),
        capability_limitations=list(capability.limitations),
    )


@_guard("world.find_openings")
def _find_openings(context: ToolContext, args: WorldSearchInput) -> OpeningsResult:
    return _openings(context, args, fresher=False)


@_guard("world.find_fresher_openings")
def _find_fresher_openings(context: ToolContext, args: WorldSearchInput) -> OpeningsResult:
    return _openings(context, args, fresher=True)


# --------------------------------------------------------------------------- derived


class SkillRow(ToolOutput):
    skill: str
    openings_mentioning: int
    sample_size: int


class SkillDemandResult(ToolOutput):
    available: bool = True
    skills: list[SkillRow] = Field(default_factory=list)
    sample: Optional[SampleInfo] = None
    provenance: Optional[Provenance] = None
    capability_limitations: list[str] = Field(default_factory=list)
    truncated: bool = False
    #: Present only when `available` is False.
    reason: Optional[str] = None
    evidence: Optional[str] = None
    remediation: Optional[str] = None


def _skill_result(observation, capability) -> SkillDemandResult:
    if isinstance(observation, Unknown):
        blank = _unknown(observation)
        return SkillDemandResult(
            available=False,
            reason=blank.reason,
            evidence=blank.evidence,
            remediation=blank.remediation,
            capability_limitations=list(capability.limitations),
        )
    rows = list(observation.value)[:MAX_WORLD_ITEMS]
    return SkillDemandResult(
        available=True,
        skills=[
            SkillRow(skill=row.skill, openings_mentioning=row.openings_mentioning, sample_size=row.sample_size)
            for row in rows
        ],
        sample=_sample(observation),
        provenance=_provenance(observation),
        capability_limitations=list(capability.limitations),
        truncated=len(observation.value) > len(rows),
    )


@_guard("world.get_skill_demand")
def _get_skill_demand(context: ToolContext, args: WorldSearchInput) -> SkillDemandResult:
    observation = job_market.skill_demand(context.db, _provider(), _query(args))
    return _skill_result(observation, declaration(Capability.SKILL_DEMAND))


@_guard("world.get_role_requirements")
def _get_role_requirements(context: ToolContext, args: WorldSearchInput) -> SkillDemandResult:
    observation = job_market.role_requirements(context.db, _provider(), _query(args))
    return _skill_result(observation, declaration(Capability.ROLE_REQUIREMENTS))


class CompanyRow(ToolOutput):
    company: str
    openings_in_sample: int
    sample_size: int
    titles: list[str] = Field(default_factory=list)


class CompanyHiringResult(ToolOutput):
    available: bool = True
    companies: list[CompanyRow] = Field(default_factory=list)
    sample: Optional[SampleInfo] = None
    provenance: Optional[Provenance] = None
    capability_limitations: list[str] = Field(default_factory=list)
    truncated: bool = False
    reason: Optional[str] = None
    evidence: Optional[str] = None
    remediation: Optional[str] = None


@_guard("world.get_company_hiring")
def _get_company_hiring(context: ToolContext, args: WorldSearchInput) -> CompanyHiringResult:
    capability = declaration(Capability.COMPANY_HIRING)
    observation = job_market.company_hiring(context.db, _provider(), _query(args))
    if isinstance(observation, Unknown):
        blank = _unknown(observation)
        return CompanyHiringResult(
            available=False,
            reason=blank.reason,
            evidence=blank.evidence,
            remediation=blank.remediation,
            capability_limitations=list(capability.limitations),
        )
    rows = list(observation.value)[:MAX_WORLD_ITEMS]
    return CompanyHiringResult(
        available=True,
        companies=[
            CompanyRow(
                company=row.company,
                openings_in_sample=row.openings_in_sample,
                sample_size=row.sample_size,
                titles=list(row.titles)[:5],
            )
            for row in rows
        ],
        sample=_sample(observation),
        provenance=_provenance(observation),
        capability_limitations=list(capability.limitations),
        truncated=len(observation.value) > len(rows),
    )


class CompareInput(ToolInput):
    role: Optional[str] = Field(default=None, max_length=120)
    #: Two or three named places. Bounded because each one costs a provider call against the
    #: same daily budget, so an unbounded list is a way to spend the deployment's quota.
    locations: list[str] = Field(min_length=2, max_length=job_market.MAX_COMPARISON_LOCATIONS)
    experience_level: Optional[str] = Field(default=None, max_length=30)
    limit: int = Field(default=MAX_WORLD_ITEMS, ge=1, le=MAX_WORLD_ITEMS)


class ComparisonResult(ToolOutput):
    available: bool = True
    role: Optional[str] = None
    openings_by_location: dict[str, int] = Field(default_factory=dict)
    sample_sizes: dict[str, int] = Field(default_factory=dict)
    sample: Optional[SampleInfo] = None
    provenance: Optional[Provenance] = None
    capability_limitations: list[str] = Field(default_factory=list)
    reason: Optional[str] = None
    evidence: Optional[str] = None
    remediation: Optional[str] = None


@_guard("world.compare_locations")
def _compare_locations(context: ToolContext, args: CompareInput) -> ComparisonResult:
    capability = declaration(Capability.GEOGRAPHIC_COMPARISON)
    query = job_market.WorldQuery(
        keywords=args.role, experience_level=args.experience_level, limit=args.limit
    )
    observation = job_market.compare_locations(context.db, _provider(), query, args.locations)
    if isinstance(observation, Unknown):
        blank = _unknown(observation)
        return ComparisonResult(
            available=False,
            role=args.role,
            reason=blank.reason,
            evidence=blank.evidence,
            remediation=blank.remediation,
            capability_limitations=list(capability.limitations),
        )
    return ComparisonResult(
        available=True,
        role=observation.value.skill_or_role,
        openings_by_location=dict(observation.value.by_location),
        sample_sizes=dict(observation.value.sample_sizes),
        sample=_sample(observation),
        provenance=_provenance(observation),
        capability_limitations=list(capability.limitations),
    )


# --------------------------------------------------------------------------- the catalogue


def world_specs() -> list[ToolSpec]:
    """VA-5's world capabilities, all READ.

    `world.learning_resources` is absent, and that absence is the point: no approved retrieval
    source for it exists in this repository, so there is nothing to register. It is declared
    unsupported in `app/world/capabilities.py` with the reason, rather than registered and
    answered from a model's recollection of a URL.
    """
    read = dict(
        kind=ToolKind.READ,
        idempotency=Idempotency.NATURAL,
        verification=Verification.NONE,
        # Declared because it is true: spending a provider call records it against the shared
        # per-day counter in `provider_budget`. No user data, invisible to the student, and
        # skipping it would overspend a shared API key -- but it is a write, and a tool that
        # claimed otherwise would be lying about its own behaviour.
        metering_effects=("ProviderCallBudget",),
    )
    # Longer than a personal read tool's: this spends an outbound HTTP call with its own
    # 10s-per-attempt timeout and one retry inside the provider transport.
    timeout = 30.0
    return [
        ToolSpec(
            name="world.find_openings",
            summary="Live job openings from the configured public job source, with provenance and freshness.",
            input_model=WorldSearchInput,
            output_model=OpeningsResult,
            handler=_find_openings,
            max_items=MAX_WORLD_ITEMS,
            timeout_seconds=timeout,
            **read,
        ),
        ToolSpec(
            name="world.find_fresher_openings",
            summary="Live entry-level and fresher openings, filtered to that experience level.",
            input_model=WorldSearchInput,
            output_model=OpeningsResult,
            handler=_find_fresher_openings,
            max_items=MAX_WORLD_ITEMS,
            timeout_seconds=timeout,
            **read,
        ),
        ToolSpec(
            name="world.get_role_requirements",
            summary="Skills that adverts for a role actually ask for, with the sample they came from.",
            input_model=WorldSearchInput,
            output_model=SkillDemandResult,
            handler=_get_role_requirements,
            max_items=MAX_WORLD_ITEMS,
            timeout_seconds=timeout,
            **read,
        ),
        ToolSpec(
            name="world.get_skill_demand",
            summary="How often each skill appears across one retrieved sample of adverts.",
            input_model=WorldSearchInput,
            output_model=SkillDemandResult,
            handler=_get_skill_demand,
            max_items=MAX_WORLD_ITEMS,
            timeout_seconds=timeout,
            **read,
        ),
        ToolSpec(
            name="world.get_company_hiring",
            summary="Companies advertising in one retrieved sample, and how many adverts each has.",
            input_model=WorldSearchInput,
            output_model=CompanyHiringResult,
            handler=_get_company_hiring,
            max_items=MAX_WORLD_ITEMS,
            timeout_seconds=timeout,
            **read,
        ),
        ToolSpec(
            name="world.compare_locations",
            summary="Advert counts for a role across two or three named locations, each sample sized.",
            input_model=CompareInput,
            output_model=ComparisonResult,
            handler=_compare_locations,
            max_items=job_market.MAX_COMPARISON_LOCATIONS,
            # One provider call per location, each with its own retry budget.
            timeout_seconds=60.0,
            **read,
        ),
    ]
