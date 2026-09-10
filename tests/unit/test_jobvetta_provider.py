"""Jobvetta adapter contract tests.

NOT LIVE, and less live than the other adapters' tests. Jobvetta's *request* contract was
supplied by the owner (base URL, bearer auth, GET /jobs with q/location/days/limit, GET
/jobs/{id}, 50 requests per key per UTC day, India-only coverage); its *response* schema was
not. So the payload fixtures below are shaped from plausible field names collected in the
adapter's own `_FIELD_ALIASES` table, and what they prove is that the adapter maps, refuses and
fails correctly for a given shape -- not that the live API returns that shape.

That distinction is load-bearing and is why the last class here exists: whatever the real field
names turn out to be, an unrecognised record must be dropped with a warning, never filled in
with a placeholder. A wrong guess in the alias table produces an empty screen and a loud log
line, which is recoverable; a guess that invented a job title would not be.

Confirming the real contract needs one authenticated request with a real key, which is an
explicit open gate in the report rather than something asserted here.
"""

import datetime

import httpx
import pytest

from app.services.job_hunt.providers.base import (
    JobProviderAuthError,
    JobProviderNotConfiguredError,
    JobProviderRateLimitError,
    JobProviderTimeoutError,
    JobProviderUnavailableError,
    ProviderSearchQuery,
)
from app.services.job_hunt.providers.jobvetta import JobvettaJobProvider

API_KEY = "test-key-not-a-real-credential"

PAYLOAD = {
    "total": 137,
    "jobs": [
        {
            "id": "jv_88213",
            "title": "Data Engineer",
            "company": "Acme Technologies",
            "location": "Bengaluru, Karnataka",
            "description": "Airflow, Spark and PostgreSQL.",
            "url": "https://www.jobvetta.com/jobs/jv_88213",
            "posted_at": "2026-09-05T09:30:00Z",
            "employment_type": "full_time",
            "work_mode": "hybrid",
            "salary_min": 1200000,
            "salary_max": 1800000,
            "salary_currency": "INR",
            "salary_period": "year",
        }
    ],
}

QUERY = ProviderSearchQuery(
    keywords="data engineer",
    location="Bengaluru",
    page=1,
    page_size=20,
    end_user_ip="203.0.113.9",
    end_user_agent="AiPlacementOS/1.0 (Android)",
)


class Recorder:
    """Stands in for httpx.get and keeps what the adapter actually sent."""

    def __init__(self, payload=None, status_code=200, headers=None, exc=None):
        self.payload = payload if payload is not None else PAYLOAD
        self.status_code = status_code
        self.headers = headers or {}
        self.exc = exc
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        if self.exc is not None:
            raise self.exc
        return httpx.Response(
            self.status_code,
            json=self.payload,
            headers=self.headers,
            request=httpx.Request("GET", url),
        )


@pytest.fixture()
def recorder(monkeypatch):
    def _install(**kwargs):
        rec = Recorder(**kwargs)
        monkeypatch.setattr(httpx, "get", rec)
        return rec

    return _install


def provider(**kwargs) -> JobvettaJobProvider:
    return JobvettaJobProvider(api_key=kwargs.pop("api_key", API_KEY), **kwargs)


# -- Configuration ----------------------------------------------------------------------------


class TestItStaysDisabledUntilAKeyIsConfigured:
    def test_no_key_raises_not_configured_rather_than_returning_nothing(self, recorder):
        """"Not set up yet" and "no jobs match you" are different facts, and only one of them
        is a claim about the labour market. Collapsing them is the failure this whole error
        class exists to prevent."""
        rec = recorder()

        with pytest.raises(JobProviderNotConfiguredError):
            provider(api_key=None).search(QUERY)

        assert rec.calls == [], "no request may be attempted without a key"

    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_a_blank_key_counts_as_no_key(self, recorder, missing):
        recorder()
        with pytest.raises(JobProviderNotConfiguredError):
            provider(api_key=missing).search(QUERY)

    def test_the_details_endpoint_is_gated_the_same_way(self, recorder):
        recorder()
        with pytest.raises(JobProviderNotConfiguredError):
            provider(api_key=None).fetch_job_details("jv_1")

    def test_the_not_configured_message_does_not_contain_a_key(self, recorder):
        recorder()
        with pytest.raises(JobProviderNotConfiguredError) as caught:
            provider(api_key=None).search(QUERY)
        assert "JOBVETTA_API_KEY" in str(caught.value), "it should name the setting"
        assert API_KEY not in str(caught.value)


# -- The request ------------------------------------------------------------------------------


class TestItSendsExactlyTheDocumentedRequest:
    def test_the_key_travels_in_the_authorization_header_and_nowhere_else(self, recorder):
        rec = recorder()

        provider().search(QUERY)

        call = rec.calls[0]
        assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
        # Not in the URL, where it would land in access logs and proxy logs, and not in a
        # query parameter, where it would do the same.
        assert API_KEY not in call["url"]
        assert API_KEY not in str(call["params"])

    def test_it_calls_the_documented_endpoint(self, recorder):
        rec = recorder()

        provider().search(QUERY)

        assert rec.calls[0]["url"] == "https://api.jobvetta.com/v1/jobs"

    def test_it_sends_only_the_four_documented_parameters(self, recorder):
        """The contract lists q, location, days and limit. Anything else would be invented,
        and an invented parameter is either ignored or an error -- neither worth guessing at."""
        rec = recorder()

        provider().search(
            ProviderSearchQuery(
                keywords="data engineer",
                location="Bengaluru",
                max_days_old=14,
                # None of these have a documented Jobvetta parameter. They are not lost: the
                # matching engine scores against them instead of filtering on them.
                remote_only=True,
                employment_type="full_time",
                experience_level="fresher",
                salary_min=800000,
                page=1,
                page_size=10,
            )
        )

        assert set(rec.calls[0]["params"]) <= {"q", "location", "days", "limit"}
        assert rec.calls[0]["params"]["q"] == "data engineer"
        assert rec.calls[0]["params"]["days"] == 14

    @pytest.mark.parametrize(
        "page_size, expected_limit",
        [(1, 1), (5, 5), (10, 10), (20, 10), (50, 10), (0, 1)],
    )
    def test_limit_is_clamped_into_the_documented_range(self, recorder, page_size, expected_limit):
        """1-10 is the contract, and the rest of Job Hunt defaults to a page of 20. Sending 20
        would be outside the contract; clamping keeps the request valid and the shortfall is
        visible to the student as a short page rather than as an error."""
        rec = recorder()

        provider().search(ProviderSearchQuery(keywords="x", page=1, page_size=page_size))

        assert rec.calls[0]["params"]["limit"] == expected_limit

    def test_an_absent_filter_is_omitted_rather_than_sent_empty(self, recorder):
        rec = recorder()

        provider().search(ProviderSearchQuery(keywords=None, location=None, page=1, page_size=5))

        assert "q" not in rec.calls[0]["params"]
        assert "location" not in rec.calls[0]["params"]
        assert "days" not in rec.calls[0]["params"]


class TestThereIsNoPaginationSoNoneIsInvented:
    @pytest.mark.parametrize("page", [2, 3, 10])
    def test_a_page_past_the_first_returns_empty_without_calling_the_provider(
        self, recorder, page
    ):
        """Jobvetta documents no page or cursor parameter. Re-sending the identical request
        would return the identical adverts and spend another of fifty daily calls to do it, so
        the adapter answers locally instead."""
        rec = recorder()

        result = provider().search(ProviderSearchQuery(keywords="x", page=page, page_size=10))

        assert result.jobs == []
        assert result.total_available is None
        assert rec.calls == [], "page two must cost nothing"

    def test_the_first_page_does_call_the_provider(self, recorder):
        rec = recorder()

        provider().search(ProviderSearchQuery(keywords="x", page=1, page_size=10))

        assert len(rec.calls) == 1


class TestItDoesNotRetry:
    @pytest.mark.parametrize(
        "status_code", [500, 502, 503]
    )
    def test_a_server_error_is_one_request_not_two(self, recorder, status_code):
        """Every other adapter here retries once on a 5xx, which is right on a budget of
        thousands. On fifty a day, a retry spends four percent of the day's capacity re-asking
        a question that just failed."""
        rec = recorder(status_code=status_code, payload={})

        with pytest.raises(JobProviderUnavailableError):
            provider().search(QUERY)

        assert len(rec.calls) == 1

    def test_a_timeout_is_one_request_too(self, recorder):
        rec = recorder(exc=httpx.ReadTimeout("too slow"))

        with pytest.raises(JobProviderTimeoutError):
            provider().search(QUERY)

        assert len(rec.calls) == 1


# -- Failures ---------------------------------------------------------------------------------


class TestEachFailureIsItsOwnKind:
    @pytest.mark.parametrize("status_code", [401, 403])
    def test_a_rejected_key_is_an_auth_error_not_an_empty_market(self, recorder, status_code):
        recorder(status_code=status_code, payload={"error": "invalid key"})

        with pytest.raises(JobProviderAuthError):
            provider().search(QUERY)

    def test_a_404_on_search_is_an_outage_not_an_empty_market(self, recorder):
        """A 404 on the search path means the endpoint is not where we think it is -- a
        deployment or contract problem, not "there are no jobs". The details path treats 404
        as a normal answer, and that difference is deliberate: there, one advert is genuinely
        gone."""
        recorder(status_code=404, payload={})

        with pytest.raises(JobProviderUnavailableError):
            provider().search(QUERY)

    def test_a_429_is_a_rate_limit(self, recorder):
        recorder(status_code=429, payload={}, headers={"Retry-After": "600"})

        with pytest.raises(JobProviderRateLimitError):
            provider().search(QUERY)

    def test_a_retry_after_header_does_not_reach_the_error_shown_to_a_student(self, recorder):
        """Logged for an operator, not surfaced. The client's message for a 429 already says
        "try again in a few minutes"; a provider-supplied number in a mobile UI is one more
        thing that can be wrong."""
        recorder(status_code=429, payload={}, headers={"Retry-After": "600"})

        with pytest.raises(JobProviderRateLimitError) as caught:
            provider().search(QUERY)

        assert "600" not in str(caught.value)

    def test_a_non_json_body_is_an_outage_not_a_parse_crash(self, monkeypatch):
        def _get(url, params=None, headers=None, timeout=None, **kwargs):
            return httpx.Response(200, text="<html>maintenance</html>", request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", _get)

        with pytest.raises(JobProviderUnavailableError):
            provider().search(QUERY)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"unexpected": "shape"}, id="no job list anywhere"),
            pytest.param({"jobs": "not a list"}, id="jobs is not a list"),
            pytest.param(42, id="not an object at all"),
        ],
    )
    def test_an_unrecognisable_payload_is_an_outage_not_an_empty_result(self, recorder, payload):
        """The difference that matters: an empty list from the provider means "we looked and
        found nothing", and a payload we cannot read means "we could not look". Reporting the
        second as the first would tell a student there is no work for them."""
        recorder(payload=payload)

        with pytest.raises(JobProviderUnavailableError):
            provider().search(QUERY)

    def test_a_connection_failure_is_an_outage(self, recorder):
        recorder(exc=httpx.ConnectError("no route to host"))

        with pytest.raises(JobProviderUnavailableError):
            provider().search(QUERY)

    def test_no_failure_message_leaks_the_key(self, recorder):
        for kwargs in (
            {"status_code": 401, "payload": {}},
            {"status_code": 429, "payload": {}},
            {"status_code": 500, "payload": {}},
            {"exc": httpx.ConnectError("boom")},
            {"exc": httpx.ReadTimeout("slow")},
        ):
            recorder(**kwargs)
            with pytest.raises(Exception) as caught:
                provider().search(QUERY)
            assert API_KEY not in str(caught.value), kwargs


# -- Mapping ------------------------------------------------------------------------------------


class TestItMapsOnlyWhatTheProviderActuallySaid:
    def test_a_documented_shaped_payload_becomes_provider_jobs(self, recorder):
        recorder()

        result = provider().search(QUERY)

        assert result.total_available == 137
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job.provider == "jobvetta"
        assert job.provider_job_id == "jv_88213"
        assert job.url == "https://www.jobvetta.com/jobs/jv_88213"
        assert job.title == "Data Engineer"
        assert job.company == "Acme Technologies"
        assert job.location == "Bengaluru, Karnataka"
        assert job.employment_type == "full_time"
        assert job.work_mode == "hybrid"
        assert job.salary_min == 1_200_000
        assert job.salary_max == 1_800_000
        assert job.salary_currency == "INR"
        assert job.salary_period == "year"
        assert job.posted_at == datetime.datetime(2026, 9, 5, 9, 30, 0)

    def test_a_salary_is_never_marked_predicted_because_nothing_says_it_is(self, recorder):
        """Jobvetta's contract describes no modelled figure, so every salary here is one the
        advert quoted. Flagging one as an estimate would be inventing a caveat; flagging a
        modelled one as advertised would be inventing a fact."""
        recorder()
        assert provider().search(QUERY).jobs[0].salary_is_predicted is False

    def test_missing_fields_stay_none_rather_than_being_guessed(self, recorder):
        recorder(
            payload={
                "jobs": [
                    {"id": "1", "title": "Engineer", "url": "https://jobvetta.com/jobs/1"}
                ]
            }
        )

        job = provider().search(QUERY).jobs[0]

        assert job.company is None
        assert job.location is None
        assert job.work_mode is None
        assert job.employment_type is None
        assert job.seniority is None
        assert job.salary_min is None
        assert job.salary_currency is None
        assert job.salary_period is None
        assert job.description is None
        assert job.posted_at is None

    def test_a_currency_without_a_figure_is_dropped(self, recorder):
        """A currency next to no number implies we know something about the pay. We do not."""
        recorder(
            payload={
                "jobs": [
                    {
                        "id": "1",
                        "title": "Engineer",
                        "url": "https://jobvetta.com/jobs/1",
                        "salary_currency": "INR",
                        "salary_period": "year",
                    }
                ]
            }
        )

        job = provider().search(QUERY).jobs[0]

        assert job.salary_currency is None
        assert job.salary_period is None

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("remote", "remote"),
            ("Work From Home", "remote"),
            ("HYBRID", "hybrid"),
            ("on-site", "onsite"),
            ("flexible-ish", None),
            ("", None),
            (123, None),
        ],
    )
    def test_an_unrecognised_work_mode_becomes_unknown_not_onsite(self, recorder, raw, expected):
        """Defaulting to the most common value would tell a student a remote job requires
        relocation -- a fabricated fact about a real advert."""
        recorder(
            payload={
                "jobs": [
                    {
                        "id": "1",
                        "title": "Engineer",
                        "url": "https://jobvetta.com/jobs/1",
                        "work_mode": raw,
                    }
                ]
            }
        )

        assert provider().search(QUERY).jobs[0].work_mode == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [("monthly", "month"), ("PER-YEAR", None), ("hour", "hour"), ("fortnightly", None)],
    )
    def test_an_unrecognised_salary_period_becomes_unknown(self, recorder, raw, expected):
        """Bucketing an unknown period into "year" would misstate an hourly rate by roughly
        two thousand times."""
        recorder(
            payload={
                "jobs": [
                    {
                        "id": "1",
                        "title": "Engineer",
                        "url": "https://jobvetta.com/jobs/1",
                        "salary_min": 500,
                        "salary_period": raw,
                    }
                ]
            }
        )

        assert provider().search(QUERY).jobs[0].salary_period == expected

    def test_a_missing_total_stays_none_rather_than_becoming_the_page_size(self, recorder):
        """Defaulting to len(jobs) would make one page of many look like the whole result."""
        recorder(payload={"jobs": PAYLOAD["jobs"]})

        assert provider().search(QUERY).total_available is None

    def test_a_bare_list_response_is_accepted(self, recorder):
        """The envelope shape is one of the unverified parts of the contract, so a top-level
        list is handled rather than treated as unusable."""
        recorder(payload=PAYLOAD["jobs"])

        result = provider().search(QUERY)

        assert len(result.jobs) == 1
        assert result.total_available is None

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("2026-09-05T09:30:00Z", datetime.datetime(2026, 9, 5, 9, 30)),
            ("2026-09-05 09:30:00", datetime.datetime(2026, 9, 5, 9, 30)),
            ("2026-09-05", datetime.datetime(2026, 9, 5, 0, 0)),
            ("not a date", None),
            (None, None),
        ],
    )
    def test_posting_dates_parse_or_stay_unknown(self, recorder, raw, expected):
        recorder(
            payload={
                "jobs": [
                    {
                        "id": "1",
                        "title": "Engineer",
                        "url": "https://jobvetta.com/jobs/1",
                        "posted_at": raw,
                    }
                ]
            }
        )

        assert provider().search(QUERY).jobs[0].posted_at == expected

    def test_an_offset_date_is_converted_to_utc_not_stripped(self, recorder):
        """Dropping the offset would keep another timezone's wall-clock reading and shift the
        advert's apparent age by up to a day, which the "newest" sort would then act on."""
        recorder(
            payload={
                "jobs": [
                    {
                        "id": "1",
                        "title": "Engineer",
                        "url": "https://jobvetta.com/jobs/1",
                        "posted_at": "2026-09-05T09:30:00+05:30",
                    }
                ]
            }
        )

        assert provider().search(QUERY).jobs[0].posted_at == datetime.datetime(2026, 9, 5, 4, 0)


class TestARecordItCannotReadIsDroppedNotInvented:
    """The safety net under every unverified field name in the alias table.

    If the live schema turns out to use names the adapter does not know, the visible result
    must be "no results" plus a warning in the log -- recoverable, and obviously wrong to an
    operator. The unacceptable outcome is a listing built from placeholders, which a student
    would try to apply to.
    """

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param({"title": "Engineer"}, id="no url"),
            pytest.param({"url": "https://jobvetta.com/jobs/1"}, id="no title"),
            pytest.param({"url": "", "title": "Engineer"}, id="blank url"),
            pytest.param({"url": "https://x/1", "title": "   "}, id="whitespace title"),
            pytest.param("not an object", id="not an object"),
            pytest.param(None, id="null"),
        ],
    )
    def test_an_unusable_record_is_skipped(self, recorder, record):
        recorder(payload={"jobs": [record]})

        assert provider().search(QUERY).jobs == []

    def test_the_readable_records_in_a_mixed_page_still_come_through(self, recorder):
        recorder(payload={"jobs": [{"nonsense": True}, PAYLOAD["jobs"][0]]})

        jobs = provider().search(QUERY).jobs

        assert len(jobs) == 1
        assert jobs[0].title == "Data Engineer"

    def test_skipped_records_are_logged_loudly(self, recorder, caplog):
        """The symptom of a wrong alias table is an empty screen, which looks exactly like a
        quiet job market unless something says otherwise."""
        recorder(payload={"jobs": [{"nonsense": True}]})

        with caplog.at_level("WARNING"):
            provider().search(QUERY)

        assert any("skipped" in record.message for record in caplog.records)

    def test_a_record_with_no_id_falls_back_to_its_url(self, recorder):
        """provider_job_id is half of the uniqueness constraint on job_listings, so it cannot
        be blank. The URL is derived from the record rather than invented, and is stable across
        fetches, which is what the constraint needs."""
        recorder(payload={"jobs": [{"title": "Engineer", "url": "https://jobvetta.com/jobs/9"}]})

        assert provider().search(QUERY).jobs[0].provider_job_id == "https://jobvetta.com/jobs/9"


# -- Details ------------------------------------------------------------------------------------


class TestTheDetailsEndpoint:
    def test_it_calls_the_documented_path(self, recorder):
        rec = recorder(payload=PAYLOAD["jobs"][0])

        provider().fetch_job_details("jv_88213")

        assert rec.calls[0]["url"] == "https://api.jobvetta.com/v1/jobs/jv_88213"

    def test_a_missing_advert_is_none_rather_than_an_outage(self, recorder):
        """A job that has been taken down is an ordinary fact about a job board, not a
        provider failure, and must not be reported to the student as one."""
        recorder(status_code=404, payload={})

        assert provider().fetch_job_details("gone") is None

    def test_it_is_not_called_by_the_search_path(self, recorder):
        """Deliberate: one detail call per result would spend a whole day's budget of fifty on
        a single page of ten, five times over. Wiring it up is an owner decision with a
        visible cost, not an implementation detail."""
        rec = recorder()

        provider().search(QUERY)

        assert len(rec.calls) == 1
        assert rec.calls[0]["url"].endswith("/jobs")

    def test_a_blank_id_is_refused_without_a_call(self, recorder):
        rec = recorder()

        assert provider().fetch_job_details("   ") is None
        assert rec.calls == []


# -- Declared limits ------------------------------------------------------------------------------


class TestItDeclaresItsOwnCeiling:
    def test_the_daily_budget_is_the_documented_fifty(self):
        """Read by the service layer before every outbound call. If this were ever set to None
        the provider would stop being metered and the key would be spent silently."""
        assert JobvettaJobProvider(api_key=API_KEY).daily_call_budget == 50

    def test_it_carries_the_attribution_it_is_shown_with(self):
        instance = JobvettaJobProvider(api_key=API_KEY)
        assert instance.attribution_text
        assert instance.name == "jobvetta"
