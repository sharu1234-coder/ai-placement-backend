"""The World Intelligence result contracts.

Framework-free: no FastAPI, no SQLAlchemy, no provider SDK. The job-provider adapter imports
`JobProvider`; nothing here does.

The shape is driven by one requirement that is easy to state and easy to lose: **a live
observation, an internal reference value and a derived estimate must never be mistaken for one
another.** A student reading "Bangalore has more Python jobs than Pune" deserves to know
whether that came from counting adverts retrieved ten minutes ago, from a table in this
repository, or from arithmetic over a sample of twenty. So the kind of claim is part of the
type, not a note in a docstring.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Optional, TypeVar

T = TypeVar("T")


class SourceKind(str, Enum):
    """Where a value came from. The distinction that stops a guess reading as a measurement."""

    #: Retrieved from an external provider during this request. The only kind that is evidence
    #: about the world.
    LIVE_PROVIDER = "live_provider"
    #: This repository's own reference data (for example the role catalogue). True about *our
    #: model of* roles, not an observation of the market.
    INTERNAL_REFERENCE = "internal_reference"
    #: Computed from live observations in this response -- a count, a frequency, a comparison.
    #: Carries the sample it was computed from, because a number without its scope is a claim
    #: about the market rather than about the sample.
    DERIVED = "derived"


class Freshness(str, Enum):
    """How much the age of the data should temper what is said about it.

    Deliberately coarse. A precise number is already on every observation as `retrieved_at`
    and `age_seconds`; this is the judgement a consumer needs without having to know each
    category's policy.
    """

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    #: Retrieved but the source gave no publication date, so age cannot be judged. Not the
    #: same as stale: it is unknown, and saying "stale" would be inventing the bad case.
    UNDATED = "undated"


class EvidenceState(str, Enum):
    """How strongly the data supports the claim being made with it."""

    #: Directly observed from a provider, with a stable identifier.
    CONFIRMED = "confirmed"
    #: Observed, but something needed to interpret it is missing (no posting date, no company).
    PARTIAL = "partial"
    #: Sources disagree. Both are retained -- see `FieldConflict`.
    CONFLICTING = "conflicting"
    #: Too little data to say anything. The honest answer to most aggregate questions.
    INSUFFICIENT = "insufficient"
    #: No source for this at all. Different from INSUFFICIENT: there is nothing to improve by
    #: asking again.
    UNSUPPORTED = "unsupported"


class DataCategory(str, Enum):
    """What kind of thing is being retrieved. Drives the freshness policy."""

    JOB_OPENING = "job_opening"
    COMPANY_HIRING = "company_hiring"
    ROLE_REQUIREMENTS = "role_requirements"
    SKILL_DEMAND = "skill_demand"
    GEOGRAPHIC_COMPARISON = "geographic_comparison"
    LEARNING_RESOURCE = "learning_resource"


# --------------------------------------------------------------------------- provenance


@dataclass(frozen=True)
class SourceRef:
    """Who said it, and what they called it.

    `provider_record_id` is the provider's own stable identifier where it gives one. It is what
    deduplication prefers and what makes a result re-findable, so it is carried separately from
    the URL -- a URL can be rewritten by an aggregator while the identifier stays put.
    """

    provider: str
    kind: SourceKind
    #: The provider's own id for this record, when it supplies one.
    provider_record_id: Optional[str] = None
    url: Optional[str] = None
    #: Attribution the provider's terms require to be shown. Carried with the data because
    #: several providers make it a contractual condition, and an obligation that lives only in
    #: a UI component is an obligation waiting to be dropped.
    attribution_text: Optional[str] = None
    attribution_url: Optional[str] = None

    def describe(self) -> str:
        return f"{self.provider} ({self.kind.value})"


@dataclass(frozen=True)
class FieldConflict:
    """Two sources, one field, different answers -- both kept.

    The alternative is picking a winner, which manufactures a single false value and erases the
    disagreement. A consumer that must choose can; one that would rather say "sources differ"
    now can too.
    """

    field_name: str
    #: provider name -> the value that provider reported.
    values_by_provider: dict[str, Any]

    def describe(self) -> str:
        parts = ", ".join(f"{name}={value!r}" for name, value in sorted(self.values_by_provider.items()))
        return f"{self.field_name}: {parts}"


@dataclass(frozen=True)
class SampleScope:
    """What a derived number was computed over.

    Required on every `DERIVED` value. A count reported without this is a claim about the
    labour market; the same count with this attached is a claim about a specific query's
    results, which is what it actually is.
    """

    query_description: str
    providers: tuple[str, ...]
    #: Records the provider(s) returned, before deduplication.
    retrieved_count: int
    #: Records remaining after deduplication -- the real denominator.
    unique_count: int
    #: What the provider said was available in total, when it says. None means it did not, and
    #: is never defaulted to `unique_count`: that would make page one look like everything.
    total_available: Optional[int] = None
    page: int = 1
    page_size: int = 20

    def describe(self) -> str:
        total = f" of ~{self.total_available} reported available" if self.total_available is not None else ""
        return (
            f"{self.unique_count} unique of {self.retrieved_count} retrieved{total} "
            f"from {', '.join(self.providers)} for: {self.query_description}"
        )


# --------------------------------------------------------------------------- untrusted text


@dataclass(frozen=True)
class UntrustedText:
    """External prose, kept wrapped so that nothing can forget what it is.

    A job description is written by whoever posted the advert. It is not a trusted instruction,
    it is not a trusted fact, and it must not reach a model prompt without going through
    `app.ai.input_guard`. Wrapping it means the type system asks the question: a caller has to
    call `for_display()` or `for_prompt()` and, in doing so, say which boundary it is crossing.

    `for_prompt()` deliberately lives here rather than at each future call site. The input
    guard's own docstring records why the project stopped trusting per-call-site discipline:
    six hardening passes added markers and none of them stopped an input from forging one. A
    central unwrap is the version of this that a new feature cannot skip.

    VA-5 creates no prompt path. This exists so VA-6 cannot create one by accident.
    """

    text: str
    source: SourceRef

    #: Longest text carried. External prose has no inherent length, and a 200 KB description
    #: reaching a bounded tool result as-is is how a size ceiling gets hit by surprise.
    MAX_CHARACTERS = 4_000

    def __post_init__(self) -> None:
        if len(self.text) > self.MAX_CHARACTERS:
            object.__setattr__(self, "text", self.text[: self.MAX_CHARACTERS - 1] + "…")

    def for_display(self) -> str:
        """The text, for showing to a human. Still untrusted; still not an instruction."""
        return self.text

    def for_prompt(self) -> str:
        """The text, neutralised for interpolation into a model prompt.

        Escaping alone does not eliminate prompt injection and this does not claim to. What it
        does eliminate is *delimiter forgery* -- the specific, reproducible break the project
        found by red-teaming its own grading path. The rest of the defence is the prompt's own
        SECURITY RULE framing plus the output guard, neither of which belongs here.
        """
        from app.ai.input_guard import neutralize_delimiters

        return str(neutralize_delimiters(self.text))

    def __str__(self) -> str:  # pragma: no cover - guards accidental interpolation
        raise TypeError(
            "UntrustedText must not be interpolated directly; call for_display() or for_prompt()"
        )


# --------------------------------------------------------------------------- observations


@dataclass(frozen=True)
class Observation(Generic[T]):
    """One value, with everything needed to judge it."""

    value: T
    category: DataCategory
    source: SourceRef
    retrieved_at: datetime.datetime
    evidence: EvidenceState = EvidenceState.CONFIRMED
    #: When the source says the underlying thing was published or last updated. None means the
    #: source did not say, which is what makes `freshness` UNDATED.
    published_at: Optional[datetime.datetime] = None
    #: Geography the value applies to, as the query expressed it. Carried so a fallback cannot
    #: quietly answer a Pune question with Bangalore data.
    location: Optional[str] = None
    #: Things a consumer must know to read the value correctly: provider coverage limits,
    #: missing filters, a provider that ignored a parameter.
    limitations: tuple[str, ...] = ()
    conflicts: tuple[FieldConflict, ...] = ()
    #: Required when `source.kind` is DERIVED.
    scope: Optional[SampleScope] = None

    def __post_init__(self) -> None:
        if self.source.kind is SourceKind.DERIVED and self.scope is None:
            raise ValueError("a derived value must carry the sample scope it was computed from")
        if self.conflicts and self.evidence is not EvidenceState.CONFLICTING:
            raise ValueError("an observation with conflicts must be marked CONFLICTING")

    @property
    def age_seconds(self) -> Optional[float]:
        """Age of the underlying data, not of the retrieval. None when undated."""
        if self.published_at is None:
            return None
        return (self.retrieved_at - self.published_at).total_seconds()

    def freshness(self, now: Optional[datetime.datetime] = None) -> Freshness:
        from app.world.freshness import classify

        return classify(self.category, self.published_at, self.retrieved_at, now=now)


@dataclass(frozen=True)
class Unknown:
    """The answer when there is nothing honest to return.

    A distinct type rather than an empty `Observation`, because the two mean different things
    and a consumer must not be able to treat one as the other. An empty list of job openings
    says "we looked and the market is quiet". This says "we could not look", and names why.
    """

    category: DataCategory
    evidence: EvidenceState
    reason: str
    #: What would have to change for an answer to become possible. Operator-facing.
    remediation: Optional[str] = None
    attempted_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.evidence in (EvidenceState.CONFIRMED, EvidenceState.PARTIAL):
            raise ValueError("an Unknown cannot claim confirmed or partial evidence")


# --------------------------------------------------------------------------- values


@dataclass(frozen=True)
class JobOpening:
    """One advert, normalised, with no personal data and no scoring.

    Deliberately not `JobListing` (the ORM row) and deliberately not `JobResultRead` (the API
    schema). Those carry a profile-scoped match, a database id and a staleness flag computed
    against this deployment's cache. A world observation is about the advert, not about how it
    relates to one student -- that join belongs to the authenticated job-search feature.
    """

    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    employment_type: Optional[str] = None
    experience_level: Optional[str] = None
    work_mode: Optional[str] = None
    #: Normalised skill keys extracted from the advert's own text.
    skills: tuple[str, ...] = ()
    url: Optional[str] = None
    #: The advert's prose, wrapped. Omitted from tool outputs; present so a future, approved
    #: consumer has it without re-fetching.
    description: Optional[UntrustedText] = None


@dataclass(frozen=True)
class SkillDemand:
    """How often a skill appeared in one retrieved sample.

    Named `SkillDemand` because that is the question it serves, but every field is about the
    sample: `openings_mentioning` out of `sample_size`. The `Observation` carrying it is
    DERIVED and so must carry a `SampleScope`, which is what keeps "appeared in 6 of 20 adverts
    we retrieved" from becoming "30% of the market wants this".
    """

    skill: str
    openings_mentioning: int
    sample_size: int

    @property
    def share(self) -> Optional[float]:
        return self.openings_mentioning / self.sample_size if self.sample_size else None


@dataclass(frozen=True)
class CompanyHiring:
    """Openings observed for one company within one query's scope.

    Not "this company is hiring N people". A provider's search results are a sample of adverts
    matching a query, not a company's headcount plan, and the gap between those two is where a
    student would make a decision on a number nobody measured.
    """

    company: str
    openings_in_sample: int
    sample_size: int
    titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeographicComparison:
    """Two samples, side by side, with both scopes retained."""

    skill_or_role: str
    by_location: dict[str, int] = field(default_factory=dict)
    sample_sizes: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- failures


class WorldError(Exception):
    """Base for retrieval failures. Carries a `code` so a caller branches on that, not text."""

    code = "world_error"

    def safe_message(self) -> str:
        return str(self)


class SourceNotConfigured(WorldError):
    """No usable source for this capability in this deployment.

    A deployment state, not an outage, and the distinction is the whole point of
    `NullJobProvider`: "no provider configured" must never degrade into "no jobs found", which
    would be a false claim about the labour market.
    """

    code = "source_not_configured"


class SourceUnavailable(WorldError):
    """The source exists and was tried, but failed. Possibly transient."""

    code = "source_unavailable"


class SourceQuotaExhausted(WorldError):
    """The deployment's budget for this source is spent. Not fixed by the student paying."""

    code = "source_quota_exhausted"


class SourceRejectedCredentials(WorldError):
    """Credentials were refused. Operationally urgent; never shown to a student verbatim."""

    code = "source_rejected_credentials"


class CapabilityUnsupported(WorldError):
    """Asked for something no approved source can answer.

    Raised rather than faked. The product specification lists capabilities this repository has
    no source for, and inventing one would be worse than saying so.
    """

    code = "capability_unsupported"

    def __init__(self, capability: str, reason: str) -> None:
        self.capability = capability
        super().__init__(f"{capability} is not supported: {reason}")
