"""The deliberate default: no job provider configured.

This exists so that an unconfigured deployment fails *loudly and honestly* rather than
plausibly. The tempting alternatives are both product lies:

  * returning an empty list would tell a student "there are no jobs matching you", which is a
    claim about the labour market we have no evidence for;
  * returning sample or seeded listings would be fabricated data, which spec section 26
    forbids outright and which students would attempt to apply to.

So the null provider raises, the API turns that into a specific 503 with its own reason code,
and the Android client shows a "Job Hunt isn't available yet" state that is distinguishable
from both "no results" and "the provider is down". Same fail-loudly-on-first-use pattern the
codebase already applies to ANTHROPIC_API_KEY, STORAGE_S3_* and SMTP_*.
"""

from app.services.job_hunt.providers.base import (
    JobProvider,
    JobProviderNotConfiguredError,
    ProviderSearchQuery,
    ProviderSearchResult,
)


class NullJobProvider(JobProvider):
    name = "none"
    attribution_text = ""
    attribution_url = None

    def search(self, query: ProviderSearchQuery) -> ProviderSearchResult:
        raise JobProviderNotConfiguredError(
            "No job provider is configured. Set JOB_PROVIDER (and that provider's credentials) "
            "to enable Job Hunt searches."
        )
