"""Chooses the configured provider, and is the only module that knows all of them.

Mirrors app/storage/ and app/email/ exactly: one setting selects the implementation, the
concrete classes are imported here and nowhere else, and callers depend on the abstract type.
That is what lets tests substitute a fake provider through FastAPI's dependency_overrides
without touching settings, and what keeps app/services/job_hunt/service.py free of any
provider-specific import.
"""

from functools import lru_cache

from app.core.config import settings
from app.services.job_hunt.providers.adzuna import AdzunaJobProvider
from app.services.job_hunt.providers.base import JobProvider
from app.services.job_hunt.providers.careerjet import CareerjetJobProvider
from app.services.job_hunt.providers.null import NullJobProvider


def build_job_provider() -> JobProvider:
    """Construct the provider named by JOB_PROVIDER.

    An unrecognized name falls back to NullJobProvider rather than raising at import time: a
    typo'd provider name should degrade Job Hunt into its honest "not available" state, not
    prevent the whole API from starting and take every other feature down with it.
    """
    provider_name = (settings.job_provider or "none").strip().lower()

    if provider_name == "adzuna":
        return AdzunaJobProvider(
            app_id=settings.adzuna_app_id,
            app_key=settings.adzuna_app_key,
            country=settings.adzuna_country,
        )
    if provider_name == "careerjet":
        return CareerjetJobProvider(
            affid=settings.careerjet_affid,
            locale_code=settings.careerjet_locale_code,
        )
    return NullJobProvider()


@lru_cache(maxsize=1)
def _cached_provider() -> JobProvider:
    return build_job_provider()


def get_job_provider() -> JobProvider:
    """FastAPI dependency. Cached because provider instances are stateless configuration
    holders -- httpx calls are made per-request inside search(), so sharing the instance
    shares no connection state or credentials beyond what settings already hold."""
    return _cached_provider()
