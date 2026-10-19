"""Which World Intelligence capabilities actually have a source, and which do not.

This module exists because the product specification lists more than this repository can
answer, and the gap has to be visible rather than filled in. A capability appears as supported
here only if a real retrieval source exists in the code; otherwise it is declared unsupported,
with the reason, and asking for it raises `CapabilityUnsupported`.

The audit behind the table (October 2026, against the current checkout): the only outbound
career-data source in `app/` is the `JobProvider` abstraction. `app/services/github_*` reaches
GitHub, but for a *user's own* authorised account as presence evidence -- personal data, not
world data. `app/ai/ollama_provider.py` is a local model, which is knowledge rather than
observation. There is no web-search client, no documentation fetcher and no trends API.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.world.contracts import CapabilityUnsupported, DataCategory, EvidenceState


class Capability(str, Enum):
    JOB_OPENINGS = "world.job_openings"
    FRESHER_OPENINGS = "world.fresher_openings"
    COMPANY_HIRING = "world.company_hiring"
    ROLE_REQUIREMENTS = "world.role_requirements"
    SKILL_DEMAND = "world.skill_demand"
    GEOGRAPHIC_COMPARISON = "world.geographic_comparison"
    LEARNING_RESOURCES = "world.learning_resources"


@dataclass(frozen=True)
class CapabilityDeclaration:
    capability: Capability
    category: DataCategory
    supported: bool
    #: How the answer is produced, or why there is none.
    basis: str
    #: Everything a consumer must know to read a result correctly.
    limitations: tuple[str, ...] = ()
    #: Set when unsupported, so the refusal can say what would change it.
    unsupported_evidence: EvidenceState = EvidenceState.UNSUPPORTED


_COVERAGE_LIMIT = (
    "Covers only what the configured provider indexes; absence of a result is not "
    "evidence that no such job exists."
)
_SAMPLE_LIMIT = "A provider's result set is a sample matching a query, not the whole market."

DECLARATIONS: dict[Capability, CapabilityDeclaration] = {
    Capability.JOB_OPENINGS: CapabilityDeclaration(
        capability=Capability.JOB_OPENINGS,
        category=DataCategory.JOB_OPENING,
        supported=True,
        basis="Live retrieval through the configured JobProvider, deduplicated and normalised.",
        limitations=(_COVERAGE_LIMIT, _SAMPLE_LIMIT),
    ),
    Capability.FRESHER_OPENINGS: CapabilityDeclaration(
        capability=Capability.FRESHER_OPENINGS,
        category=DataCategory.JOB_OPENING,
        supported=True,
        basis=(
            "The same retrieval with experience_level='fresher', plus title-derived seniority "
            "where the provider omits it."
        ),
        limitations=(
            _COVERAGE_LIMIT,
            (
                "Experience level is often absent from an advert; where it is, seniority is "
                "inferred from the title and the result is marked PARTIAL."
            ),
            (
                "Providers differ in whether they filter on experience at all. An advert whose "
                "level contradicts the filter is dropped, not relabelled."
            ),
        ),
    ),
    Capability.ROLE_REQUIREMENTS: CapabilityDeclaration(
        capability=Capability.ROLE_REQUIREMENTS,
        category=DataCategory.ROLE_REQUIREMENTS,
        supported=True,
        basis=(
            "Skills extracted from retrieved adverts' own text by the existing "
            "normalization.extract_job_skills."
        ),
        limitations=(
            (
                "Extraction is keyword-based against a fixed vocabulary, so it under-reports "
                "skills that vocabulary does not name."
            ),
            "Reflects how employers write adverts, which is not the same as what the role needs.",
        ),
    ),
    Capability.SKILL_DEMAND: CapabilityDeclaration(
        capability=Capability.SKILL_DEMAND,
        category=DataCategory.SKILL_DEMAND,
        supported=True,
        basis="Frequency of each extracted skill across one deduplicated, scoped result sample.",
        limitations=(
            (
                "A figure about the retrieved sample, never about the market. Every result "
                "carries the SampleScope it was computed over."
            ),
            "Reported as INSUFFICIENT below the minimum sample size rather than as a percentage.",
        ),
    ),
    Capability.COMPANY_HIRING: CapabilityDeclaration(
        capability=Capability.COMPANY_HIRING,
        category=DataCategory.COMPANY_HIRING,
        supported=True,
        basis="Count of a company's adverts within one query's deduplicated results.",
        limitations=(
            "Openings observed in a sample, not a company's hiring plan or headcount.",
            (
                "A company absent from the sample may simply not be indexed by the provider "
                "for this query."
            ),
        ),
    ),
    Capability.GEOGRAPHIC_COMPARISON: CapabilityDeclaration(
        capability=Capability.GEOGRAPHIC_COMPARISON,
        category=DataCategory.GEOGRAPHIC_COMPARISON,
        supported=True,
        basis="Two or more separate retrievals, one per location, each with its own scope retained.",
        limitations=(
            (
                "Compares samples, not labour markets: provider coverage can differ by city, "
                "and that difference is indistinguishable from a real one."
            ),
            "Costs one provider call per location, metered against the same daily budget.",
        ),
    ),
    Capability.LEARNING_RESOURCES: CapabilityDeclaration(
        capability=Capability.LEARNING_RESOURCES,
        category=DataCategory.LEARNING_RESOURCE,
        supported=False,
        basis=(
            "No approved retrieval source exists. There is no web-search client, documentation "
            "fetcher or curated-resource dataset anywhere in app/, and VA-5 may not add a "
            "dependency or make unauthorised outbound calls. Returning a model's recollection "
            "of a URL would be a fabricated citation, which is the specific failure this layer "
            "is built to prevent."
        ),
    ),
}


def declaration(capability: Capability) -> CapabilityDeclaration:
    return DECLARATIONS[capability]


def require_supported(capability: Capability) -> CapabilityDeclaration:
    """Return the declaration, or raise if the capability has no source.

    Raised before any work starts, so an unsupported capability costs nothing and cannot be
    answered by accident further down.
    """
    found = DECLARATIONS[capability]
    if not found.supported:
        raise CapabilityUnsupported(capability.value, found.basis)
    return found


def supported_capabilities() -> tuple[Capability, ...]:
    return tuple(cap for cap, decl in DECLARATIONS.items() if decl.supported)


def unsupported_capabilities() -> tuple[Capability, ...]:
    return tuple(cap for cap, decl in DECLARATIONS.items() if not decl.supported)
