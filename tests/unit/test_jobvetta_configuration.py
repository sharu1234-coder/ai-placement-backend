"""How Jobvetta is selected, and where its key is allowed to appear.

Two separate concerns, both configuration rather than behaviour.

*Selection.* JOB_PROVIDER picks the source, exactly like STORAGE_PROVIDER and EMAIL_PROVIDER.
The default is "none", which is a real state rather than a broken one: Job Hunt answers a
specific 503 and the app says "not available yet". An unrecognised name must degrade to the
same honest state rather than raising at import and taking every unrelated feature down with it.

*Secrecy.* The key is a bearer credential for a third-party API with a hard daily quota. It may
exist in exactly one place -- the environment, read into settings -- and must not appear in
source control, in a log line, in an exception message, or in anything sent to a client. The
tests below check each of those, including a scan of the repository itself, because "we
remembered not to" is not a control.
"""

import logging
import pathlib
import re

import httpx
import pytest

from app.core.config import Settings, settings
from app.services.job_hunt.providers import factory
from app.services.job_hunt.providers.adzuna import AdzunaJobProvider
from app.services.job_hunt.providers.base import ProviderSearchQuery
from app.services.job_hunt.providers.careerjet import CareerjetJobProvider
from app.services.job_hunt.providers.jobvetta import JobvettaJobProvider
from app.services.job_hunt.providers.null import NullJobProvider

SECRET = "jv-live-0123456789abcdef-not-a-real-key"

QUERY = ProviderSearchQuery(keywords="data engineer", page=1, page_size=10)


class TestSelection:
    def test_the_default_is_no_provider_at_all(self):
        """A deployment that has configured nothing must not silently pick a source."""
        assert Settings().job_provider == "none"

    def test_no_key_is_shipped_as_a_default(self):
        """A placeholder default would make an unconfigured deployment look configured, and
        turn "not set up" into a stream of 401s from a real API.

        Read off the field rather than off an instance: constructing Settings() loads whatever
        .env the developer running the suite happens to have, so an instance says what *this
        machine* is configured with. The field default is what ships.
        """
        assert Settings.model_fields["jobvetta_api_key"].default is None

    def test_an_empty_setting_is_treated_as_unset(self, monkeypatch):
        """The state an unconfigured deployment actually lands in: the variable exists in the
        environment with nothing after the "=", which pydantic reads as "" rather than None."""
        monkeypatch.setattr(settings, "job_provider", "jobvetta")
        monkeypatch.setattr(settings, "jobvetta_api_key", "")

        from app.services.job_hunt.providers.base import JobProviderNotConfiguredError

        with pytest.raises(JobProviderNotConfiguredError):
            factory.build_job_provider().search(QUERY)

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("jobvetta", JobvettaJobProvider),
            ("JOBVETTA", JobvettaJobProvider),
            ("  jobvetta  ", JobvettaJobProvider),
            ("adzuna", AdzunaJobProvider),
            ("careerjet", CareerjetJobProvider),
            ("none", NullJobProvider),
            ("", NullJobProvider),
            ("typo-provider", NullJobProvider),
        ],
    )
    def test_the_setting_selects_the_adapter(self, monkeypatch, name, expected):
        monkeypatch.setattr(settings, "job_provider", name)

        assert isinstance(factory.build_job_provider(), expected)

    def test_jobvetta_is_constructed_even_with_no_key(self, monkeypatch):
        """Failing at construction would take the whole API down at import time over a setting
        that only affects one feature. It fails on first use instead, which is where the
        product already has an honest state to show."""
        monkeypatch.setattr(settings, "job_provider", "jobvetta")
        monkeypatch.setattr(settings, "jobvetta_api_key", None)

        built = factory.build_job_provider()

        assert isinstance(built, JobvettaJobProvider)

    def test_the_configured_key_is_the_one_that_is_used(self, monkeypatch):
        monkeypatch.setattr(settings, "job_provider", "jobvetta")
        monkeypatch.setattr(settings, "jobvetta_api_key", SECRET)
        sent = {}

        def _get(url, params=None, headers=None, timeout=None, **kwargs):
            sent.update(headers or {})
            return httpx.Response(200, json={"jobs": []}, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", _get)

        factory.build_job_provider().search(QUERY)

        assert sent["Authorization"] == f"Bearer {SECRET}"


class TestTheKeyStaysOutOfTheLogs:
    @pytest.mark.parametrize(
        "response_kwargs, exc",
        [
            ({"status_code": 401, "json": {}}, None),
            ({"status_code": 403, "json": {}}, None),
            ({"status_code": 429, "json": {}}, None),
            ({"status_code": 500, "json": {}}, None),
            ({"status_code": 200, "text": "<html>"}, None),
            ({"status_code": 200, "json": {"unexpected": 1}}, None),
            (None, httpx.ConnectError("no route")),
            (None, httpx.ReadTimeout("slow")),
        ],
    )
    def test_no_failure_path_writes_the_key_anywhere(
        self, monkeypatch, caplog, response_kwargs, exc
    ):
        """Every branch, not a representative sample: a credential leak needs only one path
        that interpolates the wrong variable, and the branch that does it is by definition the
        one nobody looked at."""

        def _get(url, params=None, headers=None, timeout=None, **kwargs):
            if exc is not None:
                raise exc
            return httpx.Response(request=httpx.Request("GET", url), **response_kwargs)

        monkeypatch.setattr(httpx, "get", _get)
        provider = JobvettaJobProvider(api_key=SECRET)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(Exception) as caught:
                provider.search(QUERY)

        assert SECRET not in str(caught.value)
        for record in caplog.records:
            assert SECRET not in record.getMessage()

    def test_a_successful_search_logs_nothing_containing_the_key(self, monkeypatch, caplog):
        def _get(url, params=None, headers=None, timeout=None, **kwargs):
            return httpx.Response(
                200,
                json={"jobs": [{"id": "1", "title": "Engineer", "url": "https://x.test/1"}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx, "get", _get)

        with caplog.at_level(logging.DEBUG):
            JobvettaJobProvider(api_key=SECRET).search(QUERY)

        for record in caplog.records:
            assert SECRET not in record.getMessage()

    def test_the_key_is_not_put_in_the_url_or_the_query_string(self, monkeypatch):
        """A credential in a URL ends up in access logs, proxy logs and browser history, none
        of which are ours to clean up afterwards."""
        seen = {}

        def _get(url, params=None, headers=None, timeout=None, **kwargs):
            seen["url"] = url
            seen["params"] = params or {}
            return httpx.Response(200, json={"jobs": []}, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", _get)

        JobvettaJobProvider(api_key=SECRET).search(QUERY)

        assert SECRET not in seen["url"]
        assert SECRET not in repr(seen["params"])


class TestNoKeyIsCommittedToSourceControl:
    """A scan rather than a promise.

    The failure this guards against is somebody pasting a working key into a module or a
    fixture "just to try it" and it surviving into a commit. Once that happens the key is
    burned regardless of what is done afterwards, so the check has to run every time rather
    than at review time.
    """

    def _repository_python_files(self):
        """Every module under app/ and tests/, except this one.

        The exclusion is not a loophole: this file is the only place that contains the pattern
        as *prose* -- the regex source and the assertion message both mention the variable name
        -- and a scanner that flags its own definition would have to be silenced some other way.
        Everything it is actually guarding is still in scope.
        """
        root = pathlib.Path(__file__).resolve().parents[2]
        here = pathlib.Path(__file__).resolve()
        for path in list((root / "app").rglob("*.py")) + list((root / "tests").rglob("*.py")):
            if path.resolve() != here:
                yield path

    def test_no_module_assigns_a_literal_jobvetta_key(self):
        # Matches an assignment of a non-empty string literal to anything key-shaped, which is
        # the form a pasted credential takes. Empty strings and None are fine -- those are the
        # "unset" states the design depends on.
        pattern = re.compile(
            r"jobvetta[_a-z]*key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']", re.IGNORECASE
        )
        offenders = []
        for path in self._repository_python_files():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if pattern.search(line) and "not-a-real" not in line and "test-key" not in line:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")

        assert offenders == [], f"a Jobvetta key literal appears in source: {offenders}"

    def test_the_env_example_ships_the_setting_but_never_a_value(self):
        """The template documents the variable so a deployment knows to set it, and leaves it
        empty so copying the template cannot accidentally configure a key."""
        root = pathlib.Path(__file__).resolve().parents[2]
        example = (root / ".env.example").read_text(encoding="utf-8")

        assert "JOBVETTA_API_KEY=" in example
        for line in example.splitlines():
            if line.startswith("JOBVETTA_API_KEY="):
                assert line.strip() == "JOBVETTA_API_KEY=", f"a value is present: {line!r}"

    def test_the_real_env_file_is_not_tracked(self):
        """.env is where a real key legitimately lives, which is exactly why it must be
        ignored -- the template above is the file that gets committed."""
        root = pathlib.Path(__file__).resolve().parents[2]
        ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()

        assert any(line.strip() in (".env", "*.env", ".env*") for line in ignored)


class TestTheKeyNeverReachesTheClient:
    """The end of the chain: whatever the server knows, the phone must not.

    Job Hunt's whole reason for proxying the provider is that the credential stays server-side.
    These drive the real HTTP API with a configured key and a provider that fails in each of
    the ways it can, and check the response body every time -- because the failure that matters
    is not "we meant to leak it", it is one handler that reached for `str(exc)`.
    """

    @pytest.fixture()
    def headers(self, client):
        client.post(
            "/auth/signup", json={"email": "keycheck@example.com", "password": "supersecret123"}
        )
        token = client.post(
            "/auth/login", json={"email": "keycheck@example.com", "password": "supersecret123"}
        ).json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        client.put(
            "/job-hunt/profile", json={"target_role_title": "Data Engineer"}, headers=auth
        )
        return auth

    @pytest.fixture()
    def jobvetta(self, client, monkeypatch):
        from app.main import app
        from app.services.job_hunt.providers.factory import get_job_provider

        monkeypatch.setattr(settings, "jobvetta_api_key", SECRET)
        app.dependency_overrides[get_job_provider] = lambda: JobvettaJobProvider(api_key=SECRET)
        yield
        app.dependency_overrides.pop(get_job_provider, None)

    @pytest.mark.parametrize(
        "response_kwargs, exc",
        [
            ({"status_code": 200, "json": {"jobs": []}}, None),
            ({"status_code": 401, "json": {"error": "bad key"}}, None),
            ({"status_code": 429, "json": {}}, None),
            ({"status_code": 500, "json": {}}, None),
            ({"status_code": 200, "text": "<html>"}, None),
            (None, httpx.ConnectError("no route")),
            (None, httpx.ReadTimeout("slow")),
        ],
    )
    def test_no_response_body_ever_contains_the_key(
        self, client, headers, jobvetta, monkeypatch, response_kwargs, exc
    ):
        def _get(url, params=None, headers=None, timeout=None, **kwargs):
            if exc is not None:
                raise exc
            return httpx.Response(request=httpx.Request("GET", url), **response_kwargs)

        monkeypatch.setattr(httpx, "get", _get)

        response = client.post("/job-hunt/search", json={}, headers=headers)

        assert SECRET not in response.text
        # And nothing about which provider failed or why -- the same rule the rest of Job Hunt
        # already follows for provider text.
        assert "Bearer" not in response.text

    def test_the_search_response_carries_attribution_but_no_credential(
        self, client, headers, jobvetta, monkeypatch
    ):
        def _get(url, params=None, headers=None, timeout=None, **kwargs):
            return httpx.Response(
                200,
                json={"jobs": [{"id": "1", "title": "Engineer", "url": "https://x.test/1"}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx, "get", _get)

        body = client.post("/job-hunt/search", json={}, headers=headers).json()

        assert body["provider"] == "jobvetta"
        assert body["attribution_text"]
        assert SECRET not in str(body)
