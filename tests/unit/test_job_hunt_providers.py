"""Provider adapter contract tests.

NOT LIVE. Every test here drives the adapters against recorded, hand-written payload fixtures
shaped from each provider's published documentation -- no network call is made and no
credentials are used. That means these tests prove the adapter handles a given shape
correctly; they do NOT prove the live API actually returns that shape. Confirming the real
contract needs a smoke test with real credentials, which is tracked as a separate, still-open
gate in the audit report. Calling these "provider verification" would be exactly the kind of
false evidence the spec forbids.

Fixtures are modelled on:
  * Adzuna    -- developer.adzuna.com/overview and /docs/terms_of_service
  * Careerjet -- careerjet.co.in/partners/api (v4 query endpoint)
"""

import datetime

import httpx
import pytest

from app.services.job_hunt.providers import base
from app.services.job_hunt.providers.adzuna import AdzunaJobProvider
from app.services.job_hunt.providers.base import (
    JobProviderAuthError,
    JobProviderNotConfiguredError,
    JobProviderRateLimitError,
    JobProviderTimeoutError,
    JobProviderUnavailableError,
    ProviderSearchQuery,
)
from app.services.job_hunt.providers.careerjet import CareerjetJobProvider
from app.services.job_hunt.providers.null import NullJobProvider

# A realistic Adzuna search response, trimmed to the fields the adapter reads.
ADZUNA_PAYLOAD = {
    "count": 812,
    "results": [
        {
            "id": "4523119876",
            "title": "Software Engineer",
            "description": "Work with Python and PostgreSQL on distributed systems.",
            "created": "2026-09-01T09:30:00Z",
            "redirect_url": "https://www.adzuna.co.in/details/4523119876",
            "company": {"display_name": "Acme Technologies"},
            "location": {"display_name": "Bengaluru, Karnataka"},
            "salary_min": 900000.0,
            "salary_max": 1600000.0,
            # Documented as the string "0"/"1", not a JSON boolean.
            "salary_is_predicted": "1",
            "contract_time": "full_time",
        }
    ],
}

CAREERJET_PAYLOAD = {
    "type": "JOBS",
    "hits": 245,
    "jobs": [
        {
            "title": "Backend Developer",
            "company": "Globex",
            "locations": "Hyderabad",
            "description": "Java and Spring Boot microservices.",
            # Careerjet uses a space separator, not ISO 8601's "T".
            "date": "2026-09-03 14:05:00",
            "url": "https://www.careerjet.co.in/jobad/inabcdef123456",
            "salary_min": 600000,
            "salary_max": 1200000,
            "salary_currency_code": "INR",
            "salary_type": "Y",
        }
    ],
}


def fake_get(payload, status_code=200):
    """Substitute for httpx.get returning one recorded response."""

    def _get(url, params=None, timeout=None, **kwargs):
        return httpx.Response(status_code, json=payload, request=httpx.Request("GET", url))

    return _get


def raising_get(exc):
    def _get(url, params=None, timeout=None, **kwargs):
        raise exc

    return _get


QUERY = ProviderSearchQuery(
    keywords="software engineer",
    location="Bengaluru",
    page=1,
    page_size=20,
    end_user_ip="203.0.113.9",
    end_user_agent="AiPlacementOS/1.0 (Android)",
)


class TestNullProvider:
    def test_raises_rather_than_returning_an_empty_list(self):
        """The whole point of this provider. Returning [] would tell a student there are no
        jobs for them, which is a claim about the labour market an unconfigured deployment has
        no evidence for."""
        with pytest.raises(JobProviderNotConfiguredError):
            NullJobProvider().search(QUERY)


class TestAdzunaAdapter:
    def test_parses_a_documented_payload_into_provider_jobs(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", fake_get(ADZUNA_PAYLOAD))
        result = AdzunaJobProvider("id", "key").search(QUERY)

        assert result.total_available == 812
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job.provider == "adzuna"
        assert job.provider_job_id == "4523119876"
        assert job.title == "Software Engineer"
        assert job.company == "Acme Technologies"
        assert job.location == "Bengaluru, Karnataka"
        assert job.employment_type == "full_time"
        assert job.salary_min == 900000
        assert job.salary_currency == "INR"
        assert job.posted_at == datetime.datetime(2026, 9, 1, 9, 30, 0)

    def test_a_predicted_salary_is_flagged_so_the_ui_can_label_it_an_estimate(self):
        """Showing a modelled figure as if the employer advertised it would be fabricated
        data. The flag arrives as the string "1", so a truthiness check on the raw value would
        also mark "0" as predicted."""
        provider = AdzunaJobProvider("id", "key")
        assert provider._parse_job({**ADZUNA_PAYLOAD["results"][0]}).salary_is_predicted is True
        not_predicted = {**ADZUNA_PAYLOAD["results"][0], "salary_is_predicted": "0"}
        assert provider._parse_job(not_predicted).salary_is_predicted is False

    def test_missing_fields_stay_none_rather_than_being_guessed(self):
        sparse = {"id": "1", "title": "Engineer", "redirect_url": "https://x.com/1"}
        job = AdzunaJobProvider("id", "key")._parse_job(sparse)
        assert job.company is None
        assert job.location is None
        assert job.salary_min is None
        assert job.posted_at is None
        # Adzuna exposes no work-mode field at all; inferring one here would present a guess
        # as provider data.
        assert job.work_mode is None

    def test_one_malformed_advert_does_not_fail_the_whole_page(self, monkeypatch):
        payload = {"count": 2, "results": [ADZUNA_PAYLOAD["results"][0], {"no_id": True}]}
        monkeypatch.setattr(httpx, "get", fake_get(payload))
        result = AdzunaJobProvider("id", "key").search(QUERY)
        assert len(result.jobs) == 1

    def test_unset_credentials_raise_not_configured_before_any_network_call(self):
        with pytest.raises(JobProviderNotConfiguredError):
            AdzunaJobProvider(None, None).search(QUERY)

    @pytest.mark.parametrize("country,expected", [("in", "INR"), ("gb", "GBP"), ("us", "USD")])
    def test_the_currency_follows_the_country_endpoint_that_was_queried(self, country, expected):
        """Adzuna's API is one endpoint per country and the response carries no currency code,
        so the country is the only thing that says what the numbers mean.

        Only "in" was mapped before, so licensing any other market would have produced a
        salary with no currency beside it -- a six-figure number that every reader would take
        to be in their own money."""
        job = AdzunaJobProvider("id", "key", country)._parse_job(ADZUNA_PAYLOAD["results"][0])
        assert job.salary_currency == expected

    def test_a_country_adzuna_has_not_launched_yields_no_currency_rather_than_a_guess(self):
        job = AdzunaJobProvider("id", "key", "zz")._parse_job(ADZUNA_PAYLOAD["results"][0])
        assert job.salary_currency is None

    @pytest.mark.parametrize("country", ["", "india", "IN/../", "i", None])
    def test_a_malformed_country_is_a_configuration_error_not_an_outage(self, country):
        """It goes straight into the URL path, so a bad value produces a 404 that the API
        layer would report to students as "the job service isn't responding" -- an outage
        message for a setting the operator can fix in one line. The two are deliberately
        different error classes everywhere else in this module, and this is no exception."""
        with pytest.raises(JobProviderNotConfiguredError):
            AdzunaJobProvider("id", "key", country).search(QUERY)

    @pytest.mark.parametrize(
        "status_code,expected",
        [
            (401, JobProviderAuthError),
            (403, JobProviderAuthError),
            (429, JobProviderRateLimitError),
            (500, JobProviderUnavailableError),
            (503, JobProviderUnavailableError),
        ],
    )
    def test_http_failures_map_to_distinct_error_classes(self, monkeypatch, status_code, expected):
        """Distinct classes matter: the API layer turns each into a different status code, so
        "quota exhausted" and "credentials revoked" stay tellable apart by the client."""
        monkeypatch.setattr(httpx, "get", fake_get({}, status_code=status_code))
        with pytest.raises(expected):
            AdzunaJobProvider("id", "key").search(QUERY)

    def test_a_timeout_is_its_own_error_class(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", raising_get(httpx.ReadTimeout("too slow")))
        with pytest.raises(JobProviderTimeoutError):
            AdzunaJobProvider("id", "key").search(QUERY)

    def test_a_connection_failure_is_unavailable_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", raising_get(httpx.ConnectError("no route")))
        with pytest.raises(JobProviderUnavailableError):
            AdzunaJobProvider("id", "key").search(QUERY)

    def test_a_payload_without_a_results_list_is_rejected_rather_than_read_as_empty(
        self, monkeypatch
    ):
        """A provider that changes shape must raise, not quietly become "no jobs found"."""
        monkeypatch.setattr(httpx, "get", fake_get({"unexpected": True}))
        with pytest.raises(JobProviderUnavailableError):
            AdzunaJobProvider("id", "key").search(QUERY)

    def test_a_genuinely_empty_result_set_is_returned_as_empty_not_raised(self, monkeypatch):
        """The counterpart to the test above: zero results is a legitimate answer and must be
        distinguishable from an outage."""
        monkeypatch.setattr(httpx, "get", fake_get({"count": 0, "results": []}))
        result = AdzunaJobProvider("id", "key").search(QUERY)
        assert result.jobs == []
        assert result.total_available == 0

    def test_page_size_is_capped_at_the_documented_maximum(self, monkeypatch):
        captured = {}

        def _get(url, params=None, timeout=None, **kwargs):
            captured.update(params or {})
            return httpx.Response(200, json={"count": 0, "results": []}, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", _get)
        AdzunaJobProvider("id", "key").search(
            ProviderSearchQuery(keywords="x", page_size=500)
        )
        assert captured["results_per_page"] == 50


class TestCareerjetAdapter:
    def test_parses_a_documented_payload(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", fake_get(CAREERJET_PAYLOAD))
        result = CareerjetJobProvider("affid123").search(QUERY)

        assert result.total_available == 245
        job = result.jobs[0]
        assert job.title == "Backend Developer"
        assert job.company == "Globex"
        assert job.location == "Hyderabad"
        assert job.salary_currency == "INR"
        assert job.salary_period == "year"
        assert job.posted_at == datetime.datetime(2026, 9, 3, 14, 5, 0)

    def test_a_stable_id_is_derived_from_the_url_because_v4_returns_none(self):
        """Careerjet's v4 response carries no per-advert id, but the uniqueness constraint and
        dedupe both need one. Derived, not invented: the same advert always yields the same
        id, and two different adverts do not collide."""
        provider = CareerjetJobProvider("affid123")
        first = provider._parse_job(CAREERJET_PAYLOAD["jobs"][0])
        second = provider._parse_job(CAREERJET_PAYLOAD["jobs"][0])
        other = provider._parse_job({**CAREERJET_PAYLOAD["jobs"][0], "url": "https://x.com/2"})

        assert first.provider_job_id == second.provider_job_id
        assert first.provider_job_id != other.provider_job_id
        assert len(first.provider_job_id) == 32

    def test_missing_end_user_identity_fails_loudly_instead_of_sending_a_placeholder(self):
        """Careerjet requires the end user's IP and user agent on every query. Substituting a
        fake value would be a silent terms violation that still returned results, so nothing
        would ever reveal the breach."""
        with pytest.raises(JobProviderNotConfiguredError):
            CareerjetJobProvider("affid123").search(ProviderSearchQuery(keywords="x"))

    def test_an_unset_affiliate_id_raises_not_configured(self):
        with pytest.raises(JobProviderNotConfiguredError):
            CareerjetJobProvider(None).search(QUERY)

    def test_an_error_body_returned_with_http_200_is_still_treated_as_a_failure(
        self, monkeypatch
    ):
        """Careerjet signals application-level failures in the body with a 200 status, so the
        HTTP status check alone is not sufficient."""
        monkeypatch.setattr(httpx, "get", fake_get({"type": "ERROR"}))
        with pytest.raises(JobProviderUnavailableError):
            CareerjetJobProvider("affid123").search(QUERY)

    def test_an_unrecognised_salary_period_stays_none_rather_than_defaulting_to_year(
        self, monkeypatch
    ):
        """Bucketing an unknown period into "year" would misstate an hourly rate by roughly
        2000x on the job card."""
        payload = {
            "type": "JOBS",
            "hits": 1,
            "jobs": [{**CAREERJET_PAYLOAD["jobs"][0], "salary_type": "???"}],
        }
        monkeypatch.setattr(httpx, "get", fake_get(payload))
        result = CareerjetJobProvider("affid123").search(QUERY)
        assert result.jobs[0].salary_period is None

    def test_the_end_user_identity_is_actually_sent(self, monkeypatch):
        captured = {}

        def _get(url, params=None, timeout=None, **kwargs):
            captured.update(params or {})
            return httpx.Response(
                200, json={"type": "JOBS", "hits": 0, "jobs": []}, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(httpx, "get", _get)
        CareerjetJobProvider("affid123").search(QUERY)
        assert captured["user_ip"] == "203.0.113.9"
        assert captured["user_agent"] == "AiPlacementOS/1.0 (Android)"
        assert captured["affid"] == "affid123"
        assert captured["locale_code"] == "en_IN"

    def test_page_is_capped_at_the_documented_ceiling_of_ten(self, monkeypatch):
        captured = {}

        def _get(url, params=None, timeout=None, **kwargs):
            captured.update(params or {})
            return httpx.Response(
                200, json={"type": "JOBS", "hits": 0, "jobs": []}, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(httpx, "get", _get)
        CareerjetJobProvider("affid123").search(
            ProviderSearchQuery(page=99, end_user_ip="1.2.3.4", end_user_agent="x")
        )
        assert captured["page"] == 10


class TestNoProviderFabricatesJobs:
    def test_every_adapter_raises_on_failure_rather_than_inventing_a_fallback(self, monkeypatch):
        """The single most important property in this file. A provider outage must never be
        papered over with sample listings a student could try to apply to."""
        monkeypatch.setattr(httpx, "get", raising_get(httpx.ConnectError("down")))
        for provider in (AdzunaJobProvider("id", "key"), CareerjetJobProvider("affid")):
            with pytest.raises(JobProviderUnavailableError):
                provider.search(QUERY)


class TestBoundedRetry:
    """Spec phase 15: bounded retries, no retry storms.

    The value of a retry is entirely in *what it refuses to retry*. These tests pin both
    halves: a transient failure gets exactly one more chance, and a failure that says
    something about our request gets none.
    """

    @staticmethod
    def _no_sleep(monkeypatch):
        # The backoff is real in production and pointless in a test; patching it keeps the
        # suite fast without weakening what is being asserted.
        monkeypatch.setattr("app.services.job_hunt.providers.base.time.sleep", lambda _: None)

    def test_a_timeout_is_retried_exactly_once_then_gives_up(self, monkeypatch):
        self._no_sleep(monkeypatch)
        calls = []

        def _get(url, params=None, timeout=None, **kwargs):
            calls.append(url)
            raise httpx.ReadTimeout("slow")

        monkeypatch.setattr(httpx, "get", _get)
        with pytest.raises(JobProviderTimeoutError):
            base.get_with_one_retry("https://x", params={}, timeout=1.0, provider_name="Fake")
        assert len(calls) == 2, "one retry, never a storm"

    def test_a_transient_failure_that_recovers_returns_the_second_response(self, monkeypatch):
        self._no_sleep(monkeypatch)
        responses = [httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": True})]

        def _get(url, params=None, timeout=None, **kwargs):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(httpx, "get", _get)
        result = base.get_with_one_retry("https://x", params={}, timeout=1.0, provider_name="Fake")
        assert result.status_code == 200

    def test_a_5xx_is_retried(self, monkeypatch):
        self._no_sleep(monkeypatch)
        calls = []

        def _get(url, params=None, timeout=None, **kwargs):
            calls.append(url)
            return httpx.Response(503)

        monkeypatch.setattr(httpx, "get", _get)
        result = base.get_with_one_retry("https://x", params={}, timeout=1.0, provider_name="Fake")
        assert result.status_code == 503
        assert len(calls) == 2

    def test_a_401_is_never_retried_because_a_second_try_cannot_fix_credentials(self, monkeypatch):
        self._no_sleep(monkeypatch)
        calls = []

        def _get(url, params=None, timeout=None, **kwargs):
            calls.append(url)
            return httpx.Response(401)

        monkeypatch.setattr(httpx, "get", _get)
        result = base.get_with_one_retry("https://x", params={}, timeout=1.0, provider_name="Fake")
        assert result.status_code == 401
        assert len(calls) == 1

    def test_a_429_is_never_retried_because_that_is_how_a_rate_limit_becomes_a_ban(
        self, monkeypatch
    ):
        self._no_sleep(monkeypatch)
        calls = []

        def _get(url, params=None, timeout=None, **kwargs):
            calls.append(url)
            return httpx.Response(429)

        monkeypatch.setattr(httpx, "get", _get)
        result = base.get_with_one_retry("https://x", params={}, timeout=1.0, provider_name="Fake")
        assert result.status_code == 429
        assert len(calls) == 1

    def test_two_attempts_plus_backoff_stay_inside_a_bounded_budget(self):
        # Guards the reasoning in base.py's own comment: a 15s per-attempt timeout with a
        # retry would push a failing search past 30s and into a reverse proxy's timeout.
        worst_case = (base.DEFAULT_TIMEOUT_SECONDS * base._MAX_ATTEMPTS) + base._RETRY_BACKOFF_SECONDS
        assert worst_case <= 25.0
