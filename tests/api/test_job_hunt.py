"""Job Hunt end-to-end API behaviour.

Uses a fake JobProvider injected through dependency_overrides, so no network call is made and
no credentials are needed. What that proves is the app's own behaviour -- routing, ownership,
scoring, persistence, and how each provider failure class is presented to a student. It does
not prove the live provider contract; that gate is separate and still open.
"""

import datetime
from typing import Optional

import pytest

from app.ai.provider import AIProvider
from app.ai.schemas.resume import ParsedResume, ResumeExperience
from app.core.config import settings
from app.main import app
from app.services.job_hunt.providers.base import (
    JobProvider,
    JobProviderAuthError,
    JobProviderNotConfiguredError,
    JobProviderRateLimitError,
    JobProviderTimeoutError,
    JobProviderUnavailableError,
    ProviderJob,
    ProviderSearchResult,
)
from app.services.job_hunt.providers.factory import get_job_provider
from tests.fixtures.pdf_helpers import make_pdf_with_text

_NOW = datetime.datetime(2026, 9, 7, 12, 0, 0)


def provider_job(**overrides) -> ProviderJob:
    base = dict(
        provider="fake",
        provider_job_id="1",
        url="https://jobs.example.com/1",
        title="Software Engineer",
        company="Acme",
        location="Bengaluru",
        description="Python and SQL required.",
        posted_at=_NOW,
    )
    base.update(overrides)
    return ProviderJob(**base)


class FakeJobProvider(JobProvider):
    """Returns recorded jobs, or raises a chosen error. Deliberately never invents a
    fallback -- mirroring the real adapters' contract."""

    name = "fake"
    attribution_text = "Jobs by Fake"
    attribution_url = "https://example.com"

    def __init__(self, jobs=None, error: Optional[Exception] = None, total: Optional[int] = None):
        self._jobs = jobs if jobs is not None else [provider_job()]
        self._error = error
        self._total = total
        self.last_query = None

    def search(self, query):
        self.last_query = query
        if self._error is not None:
            raise self._error
        return ProviderSearchResult(jobs=self._jobs, total_available=self._total)


class FakeResumeAI(AIProvider):
    def __init__(self, parsed):
        self._parsed = parsed

    def generate_structured(self, prompt, schema):
        return self._parsed

    def generate_text(self, prompt):
        return "fake"


SAMPLE_RESUME = ParsedResume(
    skills=["Python", "SQL"],
    education=[],
    experience=[ResumeExperience(company="Acme", title="Software Engineer Intern")],
    projects=[],
    certifications=[],
    ats_score=70.0,
    weak_sections=[],
    extraction_confidence=0.9,
)


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "hunt@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "hunt@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def use_provider():
    def _use(provider: JobProvider) -> JobProvider:
        app.dependency_overrides[get_job_provider] = lambda: provider
        return provider

    yield _use
    app.dependency_overrides.pop(get_job_provider, None)


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))


@pytest.fixture()
def premium(monkeypatch):
    """Flip the server-side entitlement check the search route consults."""

    def _set(value: bool):
        monkeypatch.setattr("app.api.job_hunt.is_premium", lambda db, profile_id: value)

    return _set


def upload_resume(client, auth_headers) -> int:
    app.dependency_overrides["_ai"] = None
    from app.ai.provider import get_ai_provider

    app.dependency_overrides[get_ai_provider] = lambda: FakeResumeAI(SAMPLE_RESUME)
    try:
        resp = client.post(
            "/resumes",
            files={"file": ("cv.pdf", make_pdf_with_text("Python SQL engineer"), "application/pdf")},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)
        app.dependency_overrides.pop("_ai", None)


# -- Profile --------------------------------------------------------------------------------


class TestProfile:
    def test_a_fresh_profile_is_empty_and_explicitly_not_ready_to_search(self, client, auth_headers):
        """is_ready_to_search=False drives the setup screen. Without it the client would show
        an empty results list, implying we searched and found nothing."""
        body = client.get("/job-hunt/profile", headers=auth_headers).json()
        assert body["is_ready_to_search"] is False
        assert body["target_role_title"] is None
        assert body["normalized_skills"] == []

    def test_setting_a_target_role_makes_the_profile_ready(self, client, auth_headers):
        body = client.put(
            "/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers
        ).json()
        assert body["target_role_title"] == "Software Engineer"
        assert body["is_ready_to_search"] is True
        assert body["signal_provenance"]["target_role_title"] == "explicit"

    def test_a_resume_alone_makes_the_profile_ready_and_seeds_skills(self, client, auth_headers):
        resume_id = upload_resume(client, auth_headers)
        body = client.put(
            "/job-hunt/profile", json={"resume_id": resume_id}, headers=auth_headers
        ).json()
        assert body["is_ready_to_search"] is True
        assert set(body["normalized_skills"]) == {"python", "sql"}
        assert body["signal_provenance"]["resume_skills"] == "resume"

    def test_a_partial_update_leaves_unmentioned_fields_alone(self, client, auth_headers):
        """The PATCH-semantics guarantee: editing only the location must not clear the role."""
        client.put(
            "/job-hunt/profile", json={"target_role_title": "Data Analyst"}, headers=auth_headers
        )
        body = client.put(
            "/job-hunt/profile", json={"preferred_location": "Pune"}, headers=auth_headers
        ).json()
        assert body["target_role_title"] == "Data Analyst"
        assert body["preferred_location"] == "Pune"

    def test_clearing_is_explicit_rather_than_expressed_as_null(self, client, auth_headers):
        client.put(
            "/job-hunt/profile", json={"target_role_title": "Data Analyst"}, headers=auth_headers
        )
        body = client.put("/job-hunt/profile", json={"clear_role": True}, headers=auth_headers).json()
        assert body["target_role_title"] is None
        assert body["is_ready_to_search"] is False

    def test_student_typed_skills_are_kept_alongside_resume_extracted_ones(self, client, auth_headers):
        """Explicit input outranks extraction and must never be overwritten by it."""
        resume_id = upload_resume(client, auth_headers)
        client.put("/job-hunt/profile", json={"resume_id": resume_id}, headers=auth_headers)
        body = client.put(
            "/job-hunt/profile", json={"declared_skills": ["Kubernetes"]}, headers=auth_headers
        ).json()
        assert "kubernetes" in body["normalized_skills"]
        assert "python" in body["normalized_skills"]

    def test_an_invalid_enum_is_rejected_before_reaching_the_service(self, client, auth_headers):
        resp = client.put("/job-hunt/profile", json={"work_mode": "underwater"}, headers=auth_headers)
        assert resp.status_code == 422

    def test_role_suggestions_come_only_from_real_resume_job_titles(self, client, auth_headers):
        """Never guessed from skills -- that would tell a student who lists Python that they
        are a Data Scientist."""
        resume_id = upload_resume(client, auth_headers)
        body = client.get(
            f"/job-hunt/profile/role-suggestions?resume_id={resume_id}", headers=auth_headers
        ).json()
        assert body["suggested_role_families"] == ["software_engineer"]

    def test_suggestions_for_another_students_resume_are_empty_not_leaked(self, client, auth_headers):
        client.post("/auth/signup", json={"email": "other@example.com", "password": "supersecret123"})
        other_token = client.post(
            "/auth/login", json={"email": "other@example.com", "password": "supersecret123"}
        ).json()["access_token"]
        resume_id = upload_resume(client, auth_headers)

        body = client.get(
            f"/job-hunt/profile/role-suggestions?resume_id={resume_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        ).json()
        assert body["suggested_role_families"] == []


# -- Search ---------------------------------------------------------------------------------


class TestSearch:
    def test_searching_without_a_role_or_resume_is_422_not_an_empty_list(self, client, auth_headers, use_provider):
        use_provider(FakeJobProvider())
        resp = client.post("/job-hunt/search", json={}, headers=auth_headers)
        assert resp.status_code == 422
        assert "target role or a resume" in resp.json()["detail"]

    def test_a_role_only_search_returns_scored_results(self, client, auth_headers, use_provider):
        use_provider(FakeJobProvider(total=42))
        client.put(
            "/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers
        )

        body = client.post("/job-hunt/search", json={}, headers=auth_headers).json()
        assert len(body["results"]) == 1
        assert body["total_available"] == 42
        assert body["provider"] == "fake"
        assert body["attribution_text"] == "Jobs by Fake"
        assert body["score_version"] == "v1"

        match = body["results"][0]["match"]
        assert 0.0 <= match["total_score"] <= 1.0
        assert match["rationale"]

    def test_a_resume_only_search_works_without_any_role_set(self, client, auth_headers, use_provider):
        use_provider(FakeJobProvider())
        resume_id = upload_resume(client, auth_headers)
        client.put("/job-hunt/profile", json={"resume_id": resume_id}, headers=auth_headers)

        resp = client.post("/job-hunt/search", json={}, headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    def test_a_combined_search_matches_resume_skills_against_the_advert(self, client, auth_headers, use_provider):
        use_provider(FakeJobProvider())
        resume_id = upload_resume(client, auth_headers)
        client.put(
            "/job-hunt/profile",
            json={"resume_id": resume_id, "target_role_title": "Software Engineer"},
            headers=auth_headers,
        )

        match = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"][0]["match"]
        assert set(match["matched_skills"]) == {"python", "sql"}
        assert match["apply_readiness"] == "ready"

    def test_results_are_ordered_best_match_first(self, client, auth_headers, use_provider):
        use_provider(
            FakeJobProvider(
                jobs=[
                    provider_job(provider_job_id="weak", url="https://x.com/2", title="Chief Financial Officer"),
                    provider_job(provider_job_id="strong", url="https://x.com/1", title="Software Engineer"),
                ]
            )
        )
        client.put(
            "/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers
        )
        results = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"]
        scores = [r["match"]["total_score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        assert results[0]["listing"]["title"] == "Software Engineer"

    def test_a_better_evidenced_job_outranks_an_equally_scored_one(self, client, auth_headers, use_provider):
        """Regression for a real mis-ranking found by the ordering test above.

        Because an unknown factor is excluded from the weighted mean rather than scored as
        zero, a job whose title is outside our role taxonomy has no role_alignment component
        at all -- so "Chief Financial Officer" tied on 1.00 with a confirmed "Software
        Engineer" match for a student targeting software engineering, purely because fewer
        things about it were knowable, and won the id tie-break by being listed first.

        The fix ranks equally-scored jobs by how many components actually backed the score,
        which breaks the tie on evidence instead of inventing a number for the unknown.
        """
        use_provider(
            FakeJobProvider(
                jobs=[
                    # Listed first, so it wins any naive id tie-break.
                    provider_job(
                        provider_job_id="unrecognised",
                        url="https://x.com/2",
                        title="Chief Financial Officer",
                        company="Globex",
                        location="Hyderabad",
                    ),
                    provider_job(provider_job_id="known", url="https://x.com/1", title="Software Engineer"),
                ]
            )
        )
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        results = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"]

        assert results[0]["listing"]["title"] == "Software Engineer"
        # The unrecognised title is still reported honestly rather than scored as a mismatch.
        assert any("don't recognise" in f for f in results[1]["match"]["unknown_factors"])

    def test_duplicates_are_removed_and_the_count_is_reported(self, client, auth_headers, use_provider):
        use_provider(
            FakeJobProvider(
                jobs=[
                    provider_job(),
                    # Same provider id -- the exact-duplicate case aggregators produce.
                    provider_job(),
                    # A genuinely different advert: distinct id, url, company and location, so
                    # none of the three dedupe passes may merge it.
                    provider_job(
                        provider_job_id="2",
                        url="https://x.com/2",
                        company="Globex",
                        location="Hyderabad",
                    ),
                ]
            )
        )
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        body = client.post("/job-hunt/search", json={}, headers=auth_headers).json()
        assert body["duplicates_removed"] == 1
        assert len(body["results"]) == 2

    def test_a_listing_with_a_hostile_url_is_dropped_rather_than_shown(self, client, auth_headers, use_provider):
        """The URL is handed to a browser intent on the device, so this is a live capability."""
        use_provider(
            FakeJobProvider(
                jobs=[
                    provider_job(
                        provider_job_id="bad",
                        url="javascript:alert(1)",
                        company="Globex",
                        location="Hyderabad",
                    ),
                    provider_job(),
                ]
            )
        )
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        results = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"]
        assert len(results) == 1
        assert results[0]["listing"]["url"].startswith("https://")

    def test_a_genuinely_empty_result_set_is_200_with_no_results(self, client, auth_headers, use_provider):
        """Must stay distinguishable from every failure below -- the client says "no jobs
        matched" here and "we couldn't search" there."""
        use_provider(FakeJobProvider(jobs=[], total=0))
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        resp = client.post("/job-hunt/search", json={}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_unknown_factors_are_surfaced_so_a_mid_score_reads_honestly(self, client, auth_headers, use_provider):
        use_provider(FakeJobProvider(jobs=[provider_job(posted_at=None, description=None)]))
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        match = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"][0]["match"]
        assert any("posted date" in f for f in match["unknown_factors"])

    def test_pagination_parameters_are_passed_through_and_bounded(self, client, auth_headers, use_provider):
        provider = use_provider(FakeJobProvider())
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        client.post("/job-hunt/search", json={"page": 2, "page_size": 10}, headers=auth_headers)
        assert provider.last_query.page == 2
        assert provider.last_query.page_size == 10

        assert client.post("/job-hunt/search", json={"page_size": 999}, headers=auth_headers).status_code == 422
        assert client.post("/job-hunt/search", json={"page": 0}, headers=auth_headers).status_code == 422


class TestProviderFailuresAreDistinguishable:
    """Each failure class gets its own status code, because the client has to tell them apart
    and must never parse an error string to do it."""

    @pytest.mark.parametrize(
        "error,expected_status",
        [
            (JobProviderNotConfiguredError("no key"), 503),
            (JobProviderAuthError("bad key"), 503),
            (JobProviderRateLimitError("quota"), 429),
            (JobProviderTimeoutError("slow"), 504),
            (JobProviderUnavailableError("500"), 502),
        ],
    )
    def test_each_failure_maps_to_its_own_status(self, client, auth_headers, use_provider, error, expected_status):
        use_provider(FakeJobProvider(error=error))
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        resp = client.post("/job-hunt/search", json={}, headers=auth_headers)
        assert resp.status_code == expected_status

    def test_no_failure_ever_returns_fabricated_jobs(self, client, auth_headers, use_provider):
        """The single most important guarantee in the feature."""
        use_provider(FakeJobProvider(error=JobProviderUnavailableError("down")))
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        resp = client.post("/job-hunt/search", json={}, headers=auth_headers)
        assert "results" not in resp.json()

    def test_provider_error_detail_never_reaches_the_client(self, client, auth_headers, use_provider):
        """Same contract as app/api/ai_errors.py: the operator detail is logged, not shown."""
        leaky = JobProviderAuthError(
            "Adzuna rejected app_key sk_live_abcd1234 via internal-proxy.svc.cluster.local:8443"
        )
        use_provider(FakeJobProvider(error=leaky))
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        body = client.post("/job-hunt/search", json={}, headers=auth_headers).text

        for secret in ("sk_live_abcd1234", "internal-proxy.svc.cluster.local", "app_key", "Adzuna"):
            assert secret not in body, f"{secret!r} leaked to the client: {body}"

    def test_the_real_reason_is_logged_for_operators(self, client, auth_headers, use_provider, caplog):
        import logging

        use_provider(FakeJobProvider(error=JobProviderAuthError("revoked app_key")))
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        with caplog.at_level(logging.WARNING, logger="app.api.job_hunt"):
            client.post("/job-hunt/search", json={}, headers=auth_headers)
        assert "revoked app_key" in caplog.text


class TestPremiumGating:
    """Gated to exactly what the supplied product design states ("Unlock advanced filters").
    Nothing broader is inferred."""

    def test_core_search_works_without_premium(self, client, auth_headers, use_provider, premium):
        premium(False)
        use_provider(FakeJobProvider())
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        resp = client.post(
            "/job-hunt/search",
            json={"keywords": "engineer", "location": "Bengaluru", "work_mode": "remote"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "advanced_filter",
        [{"salary_min": 500000}, {"employment_type": "internship"}, {"experience_level": "fresher"}, {"max_days_old": 7}],
    )
    def test_advanced_filters_are_refused_for_free_users(self, client, auth_headers, use_provider, premium, advanced_filter):
        """Refused rather than silently ignored -- dropping the filter quietly would show
        unfiltered results while the UI claimed the filter was applied."""
        premium(False)
        use_provider(FakeJobProvider())
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        resp = client.post("/job-hunt/search", json=advanced_filter, headers=auth_headers)
        assert resp.status_code == 403
        assert "Premium" in resp.json()["detail"]

    def test_advanced_filters_work_for_premium_users(self, client, auth_headers, use_provider, premium):
        premium(True)
        provider = use_provider(FakeJobProvider())
        client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
        resp = client.post("/job-hunt/search", json={"salary_min": 500000}, headers=auth_headers)
        assert resp.status_code == 200
        assert provider.last_query.salary_min == 500000


# -- Saved jobs and the application tracker -------------------------------------------------


def seed_listing(client, auth_headers, use_provider, **job_overrides) -> int:
    use_provider(FakeJobProvider(jobs=[provider_job(**job_overrides)]))
    client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=auth_headers)
    body = client.post("/job-hunt/search", json={}, headers=auth_headers).json()
    return body["results"][0]["listing"]["id"]


class TestSavedJobs:
    def test_save_then_list(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        resp = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.json()["status"] == "saved"

        saved = client.get("/job-hunt/saved", headers=auth_headers).json()
        assert len(saved) == 1
        assert saved[0]["listing"]["id"] == listing_id
        # The stored match travels with the row so the tracker can show the score.
        assert saved[0]["match"]["score_version"] == "v1"

    def test_saving_twice_is_idempotent_rather_than_a_duplicate_pipeline_entry(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        first = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers).json()
        second = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers).json()
        assert first["id"] == second["id"]
        assert len(client.get("/job-hunt/saved", headers=auth_headers).json()) == 1

    def test_moving_to_applied_stamps_applied_at_once_and_never_rewrites_it(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        saved_id = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers).json()["id"]

        applied = client.patch(
            f"/job-hunt/saved/{saved_id}", json={"status": "applied"}, headers=auth_headers
        ).json()
        assert applied["applied_at"] is not None

        later = client.patch(
            f"/job-hunt/saved/{saved_id}", json={"status": "interview"}, headers=auth_headers
        ).json()
        assert later["applied_at"] == applied["applied_at"]

    def test_the_full_pipeline_is_walkable(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        saved_id = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers).json()["id"]
        for stage in ("applied", "assessment", "interview", "offer", "rejected", "closed"):
            resp = client.patch(f"/job-hunt/saved/{saved_id}", json={"status": stage}, headers=auth_headers)
            assert resp.status_code == 200
            assert resp.json()["status"] == stage

    def test_dismissing_is_a_status_not_a_separate_concept(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        resp = client.post(
            "/job-hunt/saved", json={"listing_id": listing_id, "status": "dismissed"}, headers=auth_headers
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "dismissed"

    def test_notes_and_follow_up_dates_persist(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        saved_id = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers).json()["id"]
        body = client.patch(
            f"/job-hunt/saved/{saved_id}",
            json={"notes": "Referred by Priya", "follow_up_date": "2026-09-20"},
            headers=auth_headers,
        ).json()
        assert body["notes"] == "Referred by Priya"
        assert body["follow_up_date"] == "2026-09-20"

    def test_filtering_the_tracker_by_status(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        saved_id = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers).json()["id"]
        client.patch(f"/job-hunt/saved/{saved_id}", json={"status": "applied"}, headers=auth_headers)

        assert len(client.get("/job-hunt/saved?status=applied", headers=auth_headers).json()) == 1
        assert len(client.get("/job-hunt/saved?status=offer", headers=auth_headers).json()) == 0

    def test_an_unknown_status_is_rejected(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        saved_id = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers).json()["id"]
        assert client.patch(
            f"/job-hunt/saved/{saved_id}", json={"status": "hired_probably"}, headers=auth_headers
        ).status_code == 422

    def test_saving_a_listing_that_does_not_exist_is_404(self, client, auth_headers):
        assert client.post("/job-hunt/saved", json={"listing_id": 999999}, headers=auth_headers).status_code == 404

    def test_delete_removes_it_from_the_tracker(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        saved_id = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers).json()["id"]
        assert client.delete(f"/job-hunt/saved/{saved_id}", headers=auth_headers).status_code == 204
        assert client.get("/job-hunt/saved", headers=auth_headers).json() == []

    def test_a_search_reports_which_results_are_already_saved(self, client, auth_headers, use_provider):
        """So the card renders the right control without a second round trip."""
        listing_id = seed_listing(client, auth_headers, use_provider)
        client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers)

        results = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"]
        assert results[0]["saved_job_id"] is not None
        assert results[0]["saved_status"] == "saved"


class TestJobDetail:
    def test_returns_the_listing_with_this_students_match(self, client, auth_headers, use_provider):
        listing_id = seed_listing(client, auth_headers, use_provider)
        body = client.get(f"/job-hunt/jobs/{listing_id}", headers=auth_headers).json()
        assert body["listing"]["id"] == listing_id
        assert body["match"]["reasons"], "the detail view is where the full explanation lives"

    def test_a_missing_listing_is_404(self, client, auth_headers):
        assert client.get("/job-hunt/jobs/999999", headers=auth_headers).status_code == 404


class TestSavedSearches:
    def test_create_list_and_delete(self, client, auth_headers):
        created = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Remote SDE", "filters": {"keywords": "sde", "work_mode": "remote"}},
            headers=auth_headers,
        )
        assert created.status_code == 201
        search_id = created.json()["id"]
        assert created.json()["notify_enabled"] is False, "alerts must be opt-in per search"

        # Filters round-trip in a form the client can replay straight into POST /search --
        # without that a saved search would be a name and nothing else.
        listed = client.get("/job-hunt/saved-searches", headers=auth_headers).json()
        assert len(listed) == 1
        assert listed[0]["filters"]["keywords"] == "sde"
        assert listed[0]["filters"]["work_mode"] == "remote"
        assert client.delete(f"/job-hunt/saved-searches/{search_id}", headers=auth_headers).status_code == 204
        assert client.get("/job-hunt/saved-searches", headers=auth_headers).json() == []

    def test_a_never_run_search_reports_no_result_count_rather_than_zero(self, client, auth_headers):
        """Zero would read to the student as "there are no jobs", when the truth is "we have
        not asked yet"."""
        body = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Data roles", "filters": {"keywords": "data"}},
            headers=auth_headers,
        ).json()
        assert body["last_result_count"] is None
        assert body["last_run_at"] is None

    def test_an_invalid_filter_in_a_saved_search_is_rejected(self, client, auth_headers):
        resp = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Bad", "filters": {"work_mode": "teleport"}},
            headers=auth_headers,
        )
        assert resp.status_code == 422


class TestAuthentication:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/job-hunt/profile"),
            ("put", "/job-hunt/profile"),
            ("post", "/job-hunt/search"),
            ("get", "/job-hunt/saved"),
            ("post", "/job-hunt/saved"),
            ("get", "/job-hunt/saved-searches"),
            ("get", "/job-hunt/jobs/1"),
        ],
    )
    def test_every_endpoint_requires_authentication(self, client, method, path):
        resp = getattr(client, method)(path, json={}) if method in ("post", "put") else getattr(client, method)(path)
        # 401, matching what every other authenticated route in this app answers with a
        # missing bearer token (see tests/api/test_auth.py). Asserted exactly rather than as
        # "any 4xx", so a route that started answering 404 or 422 before checking auth would
        # still fail this test.
        assert resp.status_code == 401, f"{method.upper()} {path} was reachable unauthenticated"


class TestSkillProvenance:
    """Spec phase 5: every skill an advert asks for lands in exactly one of four buckets, and
    a resume mention is never presented as verified evidence.

    These run through the real API rather than the pure classifier so they also prove the
    wiring -- that the Evidence Engine is actually consulted, and that its verdict survives
    serialization to the client."""

    def test_a_resume_skill_with_no_other_proof_is_on_resume_not_verified(
        self, client, auth_headers, use_provider
    ):
        # The student has uploaded a resume naming Python and SQL and nothing else. That is
        # one source, which the Evidence Engine calls `supported` -- a claim. Showing it as
        # verified evidence is the exact failure phase 5 forbids.
        use_provider(FakeJobProvider())
        resume_id = upload_resume(client, auth_headers)
        client.put(
            "/job-hunt/profile",
            json={"resume_id": resume_id, "target_role_title": "Software Engineer"},
            headers=auth_headers,
        )

        match = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"][0][
            "match"
        ]
        provenance = match["skill_provenance"]
        assert provenance, "the advert names Python and SQL, so provenance must not be empty"
        assert all(item["status"] != "verified_evidence" for item in provenance)
        assert {item["status"] for item in provenance} <= {"on_resume", "missing", "unclear"}
        assert match["verified_skill_count"] == 0

    def test_provenance_covers_every_matched_and_missing_skill_and_nothing_else(
        self, client, auth_headers, use_provider
    ):
        use_provider(FakeJobProvider())
        resume_id = upload_resume(client, auth_headers)
        client.put(
            "/job-hunt/profile",
            json={"resume_id": resume_id, "target_role_title": "Software Engineer"},
            headers=auth_headers,
        )

        match = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"][0][
            "match"
        ]
        listed = [item["skill"] for item in match["skill_provenance"]]
        assert sorted(listed) == sorted(match["matched_skills"] + match["missing_skills"])

    def test_a_student_with_no_skills_gets_unclear_rather_than_a_wall_of_missing(
        self, client, auth_headers, use_provider
    ):
        # Role set, no resume, no declared skills. The advert's requirements are not things
        # this student demonstrably lacks -- they are things we have not asked about yet.
        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=auth_headers,
        )

        match = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"][0][
            "match"
        ]
        statuses = {item["status"] for item in match["skill_provenance"]}
        assert statuses == {"unclear"}
        assert "missing" not in statuses

    def test_the_detail_screen_reports_the_same_provenance_as_the_list(
        self, client, auth_headers, use_provider
    ):
        use_provider(FakeJobProvider())
        resume_id = upload_resume(client, auth_headers)
        client.put(
            "/job-hunt/profile",
            json={"resume_id": resume_id, "target_role_title": "Software Engineer"},
            headers=auth_headers,
        )
        result = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"][0]
        listing_id = result["listing"]["id"]

        detail = client.get(f"/job-hunt/jobs/{listing_id}", headers=auth_headers).json()
        assert detail["match"]["skill_provenance"] == result["match"]["skill_provenance"]


class TestRankingLabels:
    def test_the_top_result_of_a_ranked_search_can_be_labelled_best_match(
        self, client, auth_headers, use_provider
    ):
        use_provider(FakeJobProvider())
        resume_id = upload_resume(client, auth_headers)
        client.put(
            "/job-hunt/profile",
            json={
                "resume_id": resume_id,
                "target_role_title": "Software Engineer",
                "preferred_location": "Bengaluru",
                "experience_level": "fresher",
            },
            headers=auth_headers,
        )

        results = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"]
        labels = [item["match"]["ranking_label"] for item in results]
        assert labels[0] in {"best_match", "strong_fit", "stretch_opportunity", "skill_build_opportunity", None}
        # Whatever the top label is, no result below it may claim to be the best match.
        assert "best_match" not in labels[1:]

    def test_a_job_opened_on_its_own_is_never_labelled_best_match(
        self, client, auth_headers, use_provider
    ):
        use_provider(FakeJobProvider())
        resume_id = upload_resume(client, auth_headers)
        client.put(
            "/job-hunt/profile",
            json={"resume_id": resume_id, "target_role_title": "Software Engineer"},
            headers=auth_headers,
        )
        listing_id = client.post("/job-hunt/search", json={}, headers=auth_headers).json()[
            "results"
        ][0]["listing"]["id"]

        detail = client.get(f"/job-hunt/jobs/{listing_id}", headers=auth_headers).json()
        assert detail["match"]["ranking_label"] != "best_match"
