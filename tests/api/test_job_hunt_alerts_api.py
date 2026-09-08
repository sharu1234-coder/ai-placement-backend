"""Job alerts, application readiness, home summary and tracker provenance, through the API.

Uses the same fake provider as tests/api/test_job_hunt.py -- no network, no credentials. What
this proves is the app's own behaviour: entitlement, ownership, consent defaults and duplicate
suppression. It proves nothing about a live provider, and does not claim to.
"""

import datetime

import pytest

from app.main import app
from app.services.job_hunt.providers.factory import get_job_provider
from tests.api.test_job_hunt import FakeJobProvider, provider_job


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "alerts@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "alerts@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_headers(client):
    client.post("/auth/signup", json={"email": "other@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "other@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def use_provider():
    def _install(provider):
        app.dependency_overrides[get_job_provider] = lambda: provider

    yield _install
    app.dependency_overrides.pop(get_job_provider, None)


@pytest.fixture()
def premium(monkeypatch):
    """Force the server-side entitlement check to pass, without minting a real subscription.

    Both call sites are patched because they import the function separately: the router
    consults it for the readiness paywall, and the alerts service consults it for the alert
    quota. Patching only one would leave half the entitlement surface on the free path and the
    test would silently prove less than it claims.
    """
    import app.api.job_hunt as job_hunt_api
    import app.services.job_hunt.alerts as alerts_service

    monkeypatch.setattr(job_hunt_api, "is_premium", lambda db, profile_id: True)
    monkeypatch.setattr(alerts_service, "is_premium", lambda db, profile_id: True)


def seed_search(client, headers, name="Dubai Data Analyst", **filters):
    payload = {"name": name, "filters": filters or {"keywords": "data analyst"}}
    return client.post("/job-hunt/saved-searches", json=payload, headers=headers).json()


class TestAlertConsent:
    def test_a_new_alert_is_off_until_the_student_turns_it_on(self, client, auth_headers):
        # Creating an alert and consenting to notifications are two separate acts. Defaulting
        # to on would opt a student in by the act of setting one up.
        created = client.post(
            "/job-hunt/alerts", json={"name": "Remote Python"}, headers=auth_headers
        )
        assert created.status_code == 201
        assert created.json()["is_enabled"] is False

    def test_a_student_can_explicitly_opt_in_at_creation(self, client, auth_headers):
        created = client.post(
            "/job-hunt/alerts",
            json={"name": "Remote Python", "is_enabled": True},
            headers=auth_headers,
        )
        assert created.json()["is_enabled"] is True

    def test_an_alert_can_be_paused_and_resumed(self, client, auth_headers):
        alert_id = client.post(
            "/job-hunt/alerts",
            json={"name": "Remote Python", "is_enabled": True},
            headers=auth_headers,
        ).json()["id"]

        paused = client.patch(
            f"/job-hunt/alerts/{alert_id}", json={"is_enabled": False}, headers=auth_headers
        )
        assert paused.json()["is_enabled"] is False

        resumed = client.patch(
            f"/job-hunt/alerts/{alert_id}", json={"is_enabled": True}, headers=auth_headers
        )
        assert resumed.json()["is_enabled"] is True

    def test_deleting_an_alert_removes_it(self, client, auth_headers):
        alert_id = client.post(
            "/job-hunt/alerts", json={"name": "Remote Python"}, headers=auth_headers
        ).json()["id"]

        assert client.delete(f"/job-hunt/alerts/{alert_id}", headers=auth_headers).status_code == 204
        assert client.get("/job-hunt/alerts", headers=auth_headers).json() == []


class TestAlertValidation:
    def test_a_weekly_alert_without_a_day_is_refused_rather_than_guessed(self, client, auth_headers):
        resp = client.post(
            "/job-hunt/alerts",
            json={"name": "Weekly", "frequency": "weekly"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert "day of the week" in resp.json()["detail"]

    def test_half_a_quiet_window_is_refused(self, client, auth_headers):
        resp = client.post(
            "/job-hunt/alerts",
            json={"name": "Nights", "quiet_hours_start": 22},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_an_out_of_range_hour_is_refused_by_the_schema(self, client, auth_headers):
        resp = client.post(
            "/job-hunt/alerts", json={"name": "Bad", "hour_local": 99}, headers=auth_headers
        )
        assert resp.status_code == 422

    def test_quiet_hours_can_be_cleared_once_set(self, client, auth_headers):
        # Null means "unchanged" on a PATCH, so without an explicit flag there would be no way
        # to remove a quiet window at all.
        alert_id = client.post(
            "/job-hunt/alerts",
            json={"name": "Nights", "quiet_hours_start": 22, "quiet_hours_end": 7},
            headers=auth_headers,
        ).json()["id"]

        cleared = client.patch(
            f"/job-hunt/alerts/{alert_id}", json={"clear_quiet_hours": True}, headers=auth_headers
        ).json()
        assert cleared["quiet_hours_start"] is None
        assert cleared["quiet_hours_end"] is None


class TestAlertQuota:
    def test_the_free_tier_gets_a_real_alert_and_a_stated_limit(self, client, auth_headers):
        quota = client.get("/job-hunt/alerts/quota", headers=auth_headers).json()
        assert quota["limit"] >= 1
        assert quota["is_premium"] is False
        assert quota["used"] == 0

    def test_exceeding_the_free_limit_is_403_with_a_reason(self, client, auth_headers):
        limit = client.get("/job-hunt/alerts/quota", headers=auth_headers).json()["limit"]
        for index in range(limit):
            assert (
                client.post(
                    "/job-hunt/alerts", json={"name": f"Alert {index}"}, headers=auth_headers
                ).status_code
                == 201
            )

        refused = client.post("/job-hunt/alerts", json={"name": "One too many"}, headers=auth_headers)
        # 403 rather than 409: this is an entitlement ceiling, and the client routes it to the
        # upgrade prompt rather than to a retry.
        assert refused.status_code == 403
        assert "plan" in refused.json()["detail"]

    def test_premium_raises_the_ceiling(self, client, auth_headers, premium):
        quota = client.get("/job-hunt/alerts/quota", headers=auth_headers).json()
        assert quota["is_premium"] is True
        assert quota["limit"] > 1


class TestAlertOwnership:
    def test_one_student_cannot_read_another_students_alerts(
        self, client, auth_headers, other_headers
    ):
        client.post("/job-hunt/alerts", json={"name": "Mine"}, headers=auth_headers)
        assert client.get("/job-hunt/alerts", headers=other_headers).json() == []

    def test_one_student_cannot_edit_another_students_alert(
        self, client, auth_headers, other_headers
    ):
        alert_id = client.post(
            "/job-hunt/alerts", json={"name": "Mine"}, headers=auth_headers
        ).json()["id"]

        # 404 and not 403: confirming the id exists would itself be a leak.
        assert (
            client.patch(
                f"/job-hunt/alerts/{alert_id}", json={"is_enabled": True}, headers=other_headers
            ).status_code
            == 404
        )
        assert (
            client.delete(f"/job-hunt/alerts/{alert_id}", headers=other_headers).status_code == 404
        )

    def test_an_alert_cannot_be_attached_to_another_students_saved_search(
        self, client, auth_headers, other_headers
    ):
        search_id = seed_search(client, auth_headers)["id"]
        resp = client.post(
            "/job-hunt/alerts",
            json={"name": "Theirs", "saved_search_id": search_id},
            headers=other_headers,
        )
        assert resp.status_code == 404

    def test_alert_history_is_scoped_to_the_owner(self, client, auth_headers, other_headers):
        assert client.get("/job-hunt/alerts/history", headers=other_headers).json() == []


class TestAlertSavedSearchBinding:
    def test_an_alert_reports_the_saved_search_it_watches(self, client, auth_headers):
        search = seed_search(client, auth_headers, keywords="data engineer", location="Dubai")
        alert = client.post(
            "/job-hunt/alerts",
            json={"name": "Dubai", "saved_search_id": search["id"]},
            headers=auth_headers,
        ).json()

        assert alert["saved_search_id"] == search["id"]
        assert alert["saved_search_name"] == "Dubai Data Analyst"
        # Filters come from the search, live, so editing it changes what the alert looks for.
        assert alert["filters"]["keywords"] == "data engineer"

    def test_a_second_alert_on_the_same_search_returns_the_first_rather_than_doubling_it(
        self, client, auth_headers
    ):
        search = seed_search(client, auth_headers)
        first = client.post(
            "/job-hunt/alerts",
            json={"name": "One", "saved_search_id": search["id"]},
            headers=auth_headers,
        ).json()
        second = client.post(
            "/job-hunt/alerts",
            json={"name": "Two", "saved_search_id": search["id"]},
            headers=auth_headers,
        ).json()
        assert first["id"] == second["id"]

    def test_a_saved_search_reports_whether_it_has_an_alert(self, client, auth_headers):
        search = seed_search(client, auth_headers)
        before = client.get("/job-hunt/saved-searches", headers=auth_headers).json()[0]
        assert before["has_alert"] is False

        client.post(
            "/job-hunt/alerts",
            json={"name": "Watch", "saved_search_id": search["id"]},
            headers=auth_headers,
        )
        after = client.get("/job-hunt/saved-searches", headers=auth_headers).json()[0]
        assert after["has_alert"] is True

    def test_a_never_run_alert_reports_null_not_zero(self, client, auth_headers):
        # Zero would read as "we looked and found nothing for you".
        alert = client.post(
            "/job-hunt/alerts", json={"name": "New"}, headers=auth_headers
        ).json()
        assert alert["last_run_at"] is None
        assert alert["last_new_job_count"] is None
        assert alert["last_run_status"] is None


class TestSavedSearchRun:
    def test_running_a_saved_search_returns_results_and_records_the_run(
        self, client, auth_headers, use_provider
    ):
        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=auth_headers,
        )
        search = seed_search(client, auth_headers, keywords="software engineer")

        resp = client.post(
            f"/job-hunt/saved-searches/{search['id']}/run", headers=auth_headers
        )
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

        after = client.get("/job-hunt/saved-searches", headers=auth_headers).json()[0]
        assert after["last_run_at"] is not None
        assert after["last_success_at"] is not None
        assert after["last_error_at"] is None
        assert after["last_result_count"] == 1

    def test_a_failed_run_records_the_failure_and_keeps_the_previous_count(
        self, client, auth_headers, use_provider
    ):
        """Overwriting a real previous count with a zero on a failed run would tell the student
        their search stopped matching anything, when what happened is that we could not ask."""
        from app.services.job_hunt.providers.base import JobProviderUnavailableError

        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=auth_headers,
        )
        search = seed_search(client, auth_headers, keywords="software engineer")
        client.post(f"/job-hunt/saved-searches/{search['id']}/run", headers=auth_headers)

        use_provider(FakeJobProvider(error=JobProviderUnavailableError("down")))
        failed = client.post(
            f"/job-hunt/saved-searches/{search['id']}/run", headers=auth_headers
        )
        assert failed.status_code == 502
        # No provider text reaches the student.
        assert "down" not in failed.json()["detail"]

        after = client.get("/job-hunt/saved-searches", headers=auth_headers).json()[0]
        assert after["last_error_at"] is not None
        assert after["last_result_count"] == 1

    def test_running_another_students_saved_search_is_404(
        self, client, auth_headers, other_headers, use_provider
    ):
        use_provider(FakeJobProvider())
        search = seed_search(client, auth_headers)
        assert (
            client.post(
                f"/job-hunt/saved-searches/{search['id']}/run", headers=other_headers
            ).status_code
            == 404
        )


class TestTrackerProvenance:
    def _seed_listing(self, client, headers, use_provider):
        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=headers,
        )
        return client.post("/job-hunt/search", json={}, headers=headers).json()["results"][0][
            "listing"
        ]["id"]

    def test_a_job_saved_from_a_saved_search_names_that_search(
        self, client, auth_headers, use_provider
    ):
        listing_id = self._seed_listing(client, auth_headers, use_provider)
        search = seed_search(client, auth_headers)

        client.post(
            "/job-hunt/saved",
            json={
                "listing_id": listing_id,
                "source": "saved_search",
                "source_saved_search_id": search["id"],
            },
            headers=auth_headers,
        )
        saved = client.get("/job-hunt/saved", headers=auth_headers).json()[0]
        assert saved["source"] == "saved_search"
        assert saved["source_saved_search_name"] == "Dubai Data Analyst"

    def test_provenance_is_not_rewritten_when_the_status_changes(
        self, client, auth_headers, use_provider
    ):
        # A student who found a job through a search still found it through that search after
        # they reach the interview stage. A tracker that edits its own history to match the
        # present is worse than one that does not.
        listing_id = self._seed_listing(client, auth_headers, use_provider)
        search = seed_search(client, auth_headers)
        saved_id = client.post(
            "/job-hunt/saved",
            json={
                "listing_id": listing_id,
                "source": "saved_search",
                "source_saved_search_id": search["id"],
            },
            headers=auth_headers,
        ).json()["id"]

        client.patch(
            f"/job-hunt/saved/{saved_id}", json={"status": "interview"}, headers=auth_headers
        )
        saved = client.get("/job-hunt/saved", headers=auth_headers).json()[0]
        assert saved["status"] == "interview"
        assert saved["source"] == "saved_search"

    def test_a_plainly_saved_job_defaults_to_manual(self, client, auth_headers, use_provider):
        listing_id = self._seed_listing(client, auth_headers, use_provider)
        client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers)
        saved = client.get("/job-hunt/saved", headers=auth_headers).json()[0]
        assert saved["source"] == "manual"
        assert saved["source_saved_search_id"] is None

    def test_a_source_id_is_ignored_unless_the_source_says_saved_search(
        self, client, auth_headers, use_provider
    ):
        # Otherwise a row could claim a search it never came from.
        listing_id = self._seed_listing(client, auth_headers, use_provider)
        search = seed_search(client, auth_headers)
        client.post(
            "/job-hunt/saved",
            json={
                "listing_id": listing_id,
                "source": "recommended",
                "source_saved_search_id": search["id"],
            },
            headers=auth_headers,
        )
        saved = client.get("/job-hunt/saved", headers=auth_headers).json()[0]
        assert saved["source"] == "recommended"
        assert saved["source_saved_search_id"] is None


class TestHomeSummary:
    def test_a_new_student_gets_zeros_and_no_recommendations(self, client, auth_headers):
        home = client.get("/job-hunt/home", headers=auth_headers).json()
        assert home["new_jobs"] == 0
        assert home["best_matches"] == 0
        assert home["applied"] == 0
        assert home["recommended"] == []
        assert home["is_ready_to_search"] is False

    def test_counts_reflect_this_students_own_rows_after_a_search(
        self, client, auth_headers, use_provider
    ):
        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=auth_headers,
        )
        client.post("/job-hunt/search", json={}, headers=auth_headers)

        home = client.get("/job-hunt/home", headers=auth_headers).json()
        assert home["new_jobs"] == 1
        assert home["is_ready_to_search"] is True
        assert len(home["recommended"]) == 1

    def test_the_applied_count_only_counts_real_applications(
        self, client, auth_headers, use_provider
    ):
        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=auth_headers,
        )
        listing_id = client.post("/job-hunt/search", json={}, headers=auth_headers).json()[
            "results"
        ][0]["listing"]["id"]

        client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=auth_headers)
        assert client.get("/job-hunt/home", headers=auth_headers).json()["applied"] == 0

        saved_id = client.get("/job-hunt/saved", headers=auth_headers).json()[0]["id"]
        client.patch(
            f"/job-hunt/saved/{saved_id}", json={"status": "applied"}, headers=auth_headers
        )
        assert client.get("/job-hunt/home", headers=auth_headers).json()["applied"] == 1

    def test_a_dismissed_job_is_not_recommended_back(self, client, auth_headers, use_provider):
        # Recommending back a job the student explicitly hid is the fastest way to make a
        # recommendation feel like it is not listening.
        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=auth_headers,
        )
        listing_id = client.post("/job-hunt/search", json={}, headers=auth_headers).json()[
            "results"
        ][0]["listing"]["id"]
        client.post(
            "/job-hunt/saved",
            json={"listing_id": listing_id, "status": "dismissed"},
            headers=auth_headers,
        )

        assert client.get("/job-hunt/home", headers=auth_headers).json()["recommended"] == []

    def test_home_does_not_call_the_provider(self, client, auth_headers, use_provider):
        """Opening the app must not spend provider quota or wait on a network round trip."""
        from app.services.job_hunt.providers.base import JobProviderUnavailableError

        use_provider(FakeJobProvider(error=JobProviderUnavailableError("must not be called")))
        assert client.get("/job-hunt/home", headers=auth_headers).status_code == 200


class TestSkillAnalysis:
    def _listing(self, client, headers, use_provider):
        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=headers,
        )
        return client.post("/job-hunt/search", json={}, headers=headers).json()["results"][0][
            "listing"
        ]["id"]

    def test_every_row_carries_both_a_bucket_and_a_badge(
        self, client, auth_headers, use_provider
    ):
        listing_id = self._listing(client, auth_headers, use_provider)
        body = client.get(
            f"/job-hunt/jobs/{listing_id}/skill-analysis", headers=auth_headers
        ).json()
        assert body["rows"]
        for row in body["rows"]:
            assert row["status"] in ("verified_evidence", "on_resume", "missing", "unclear")
            assert row["strength"] in ("strong_match", "match", "skill_gap", "unclear")

    def test_skill_analysis_is_free(self, client, auth_headers, use_provider):
        # The diagnosis is not paywalled -- only the readiness breakdown is.
        listing_id = self._listing(client, auth_headers, use_provider)
        assert (
            client.get(
                f"/job-hunt/jobs/{listing_id}/skill-analysis", headers=auth_headers
            ).status_code
            == 200
        )

    def test_another_student_sees_their_own_analysis_of_the_same_shared_listing(
        self, client, auth_headers, other_headers, use_provider
    ):
        """job_listings is deliberately shared -- a job advert is public provider content and
        carries no profile_id. What must never cross accounts is the *analysis*, so this
        asserts the second student gets an analysis computed against their own empty profile
        rather than the first student's."""
        listing_id = self._listing(client, auth_headers, use_provider)

        mine = client.get(
            f"/job-hunt/jobs/{listing_id}/skill-analysis", headers=auth_headers
        ).json()
        theirs = client.get(
            f"/job-hunt/jobs/{listing_id}/skill-analysis", headers=other_headers
        ).json()

        assert mine["total_score"] != theirs["total_score"]
        # The other student has set no target role and no skills, so every skill is unclear
        # to us rather than missing -- and certainly not carried over from the first student.
        assert {row["status"] for row in theirs["rows"]} == {"unclear"}


class TestApplicationReadiness:
    def _listing(self, client, headers, use_provider):
        use_provider(FakeJobProvider())
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer", "experience_level": "fresher"},
            headers=headers,
        )
        return client.post("/job-hunt/search", json={}, headers=headers).json()["results"][0][
            "listing"
        ]["id"]

    def test_a_free_student_is_refused_with_an_upgrade_message(
        self, client, auth_headers, use_provider
    ):
        listing_id = self._listing(client, auth_headers, use_provider)
        resp = client.get(f"/job-hunt/jobs/{listing_id}/readiness", headers=auth_headers)
        assert resp.status_code == 403
        assert "Premium" in resp.json()["detail"]
        # And it says what stays free, so the refusal is informative rather than a wall.
        assert "free" in resp.json()["detail"]

    def test_a_premium_student_gets_the_breakdown(
        self, client, auth_headers, use_provider, premium
    ):
        listing_id = self._listing(client, auth_headers, use_provider)
        body = client.get(
            f"/job-hunt/jobs/{listing_id}/readiness", headers=auth_headers
        ).json()

        assert body["listing_id"] == listing_id
        assert len(body["dimensions"]) == 5
        keys = {dim["key"] for dim in body["dimensions"]}
        assert keys == {"skills", "experience", "resume", "education", "interview"}

    def test_dimensions_we_cannot_judge_come_back_null_and_are_named(
        self, client, auth_headers, use_provider, premium
    ):
        # A student with no ATS check and no mock interviews must not be told they are 0%
        # ready on either -- they have not attempted them, which is not the same thing.
        listing_id = self._listing(client, auth_headers, use_provider)
        body = client.get(
            f"/job-hunt/jobs/{listing_id}/readiness", headers=auth_headers
        ).json()

        by_key = {dim["key"]: dim for dim in body["dimensions"]}
        assert by_key["resume"]["score"] is None
        assert by_key["interview"]["score"] is None
        assert "Resume" in body["unknown_dimensions"]
        assert "Interview readiness" in body["unknown_dimensions"]

    def test_every_dimension_explains_where_its_number_came_from(
        self, client, auth_headers, use_provider, premium
    ):
        listing_id = self._listing(client, auth_headers, use_provider)
        body = client.get(
            f"/job-hunt/jobs/{listing_id}/readiness", headers=auth_headers
        ).json()
        assert all(dim["detail"] for dim in body["dimensions"])

    def test_readiness_is_computed_per_student_on_a_shared_listing(
        self, client, auth_headers, other_headers, use_provider, premium
    ):
        # Same shared-listing rule as the analysis endpoint: the advert is public, the verdict
        # about the student is not.
        listing_id = self._listing(client, auth_headers, use_provider)

        mine = client.get(f"/job-hunt/jobs/{listing_id}/readiness", headers=auth_headers).json()
        theirs = client.get(
            f"/job-hunt/jobs/{listing_id}/readiness", headers=other_headers
        ).json()

        # The first student has a target role, so their skills dimension is judged against
        # the advert; the second has nothing on file, so every skill is unclear and the
        # dimension comes back unjudged. That difference is only possible if the two were
        # computed separately.
        mine_skills = next(d for d in mine["dimensions"] if d["key"] == "skills")
        their_skills = next(d for d in theirs["dimensions"] if d["key"] == "skills")
        assert their_skills["score"] is None
        assert "Skills" in theirs["unknown_dimensions"]
        assert "Skills" not in mine["unknown_dimensions"] or mine_skills["score"] is None


class TestSortOptions:
    def test_sorting_reorders_without_changing_a_single_score(
        self, client, auth_headers, use_provider
    ):
        """A sort control that quietly changed the numbers would be untrustworthy."""
        old = datetime.datetime(2026, 8, 1, 12, 0, 0)
        new = datetime.datetime(2026, 9, 7, 12, 0, 0)
        use_provider(
            FakeJobProvider(
                jobs=[
                    provider_job(provider_job_id="1", url="https://x.test/1", posted_at=old),
                    provider_job(
                        provider_job_id="2",
                        url="https://x.test/2",
                        title="Senior Software Engineer",
                        company="Globex",
                        location="Pune",
                        posted_at=new,
                    ),
                ]
            )
        )
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=auth_headers,
        )

        best = client.post("/job-hunt/search", json={}, headers=auth_headers).json()["results"]
        newest = client.post(
            "/job-hunt/search", json={"sort": "newest"}, headers=auth_headers
        ).json()["results"]

        scores_by_id = {r["listing"]["id"]: r["match"]["total_score"] for r in best}
        for result in newest:
            assert result["match"]["total_score"] == scores_by_id[result["listing"]["id"]]

    def test_newest_puts_the_most_recent_first(self, client, auth_headers, use_provider):
        old = datetime.datetime(2026, 8, 1, 12, 0, 0)
        new = datetime.datetime(2026, 9, 7, 12, 0, 0)
        use_provider(
            FakeJobProvider(
                jobs=[
                    provider_job(provider_job_id="1", url="https://x.test/1", posted_at=old),
                    provider_job(
                        provider_job_id="2",
                        url="https://x.test/2",
                        title="Senior Software Engineer",
                        company="Globex",
                        location="Pune",
                        posted_at=new,
                    ),
                ]
            )
        )
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=auth_headers,
        )
        results = client.post(
            "/job-hunt/search", json={"sort": "newest"}, headers=auth_headers
        ).json()["results"]
        assert results[0]["listing"]["title"] == "Senior Software Engineer"

    def test_an_unknown_sort_is_refused_by_the_schema(self, client, auth_headers, use_provider):
        use_provider(FakeJobProvider())
        resp = client.post(
            "/job-hunt/search", json={"sort": "whatever"}, headers=auth_headers
        )
        assert resp.status_code == 422


class TestAlertAuthentication:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/job-hunt/alerts"),
            ("post", "/job-hunt/alerts"),
            ("get", "/job-hunt/alerts/quota"),
            ("get", "/job-hunt/alerts/history"),
            ("patch", "/job-hunt/alerts/1"),
            ("delete", "/job-hunt/alerts/1"),
            ("get", "/job-hunt/home"),
            ("get", "/job-hunt/jobs/1/readiness"),
            ("get", "/job-hunt/jobs/1/skill-analysis"),
            ("post", "/job-hunt/saved-searches/1/run"),
        ],
    )
    def test_every_new_endpoint_requires_authentication(self, client, method, path):
        # client.request rather than client.get(json=...): the GET helper takes no body, and
        # every one of these must be refused before a body would matter anyway.
        resp = client.request(method.upper(), path, json={})
        # 401 is what the current FastAPI/HTTPBearer answers for a missing token; older
        # versions answered 403, which is why this is asserted rather than assumed.
        assert resp.status_code == 401
