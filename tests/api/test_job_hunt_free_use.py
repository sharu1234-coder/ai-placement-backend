"""One free Job Hunt search, and every way someone might try to have two.

The rule is small and the ways around it are not, so this file is organised by attack rather
than by endpoint: reuse the request, change the request, change the account's session, race
two requests, replay an idempotency key, go through the saved-search door instead of the search
door, or wait for a failure and try again. Each class below is one of those.

Everything here goes through the HTTP API with a fake provider, because that is the boundary a
client actually has. A test that called entitlement.claim() directly would prove the ledger
works and prove nothing about whether the route uses it.
"""

import datetime
import uuid

import pytest

from app.api.deps import get_db
from app.main import app
from app.models.job_hunt_usage import JobHuntUsage
from app.services.job_hunt.providers.base import (
    JobProviderAuthError,
    JobProviderNotConfiguredError,
    JobProviderRateLimitError,
    JobProviderTimeoutError,
    JobProviderUnavailableError,
)
from app.services.job_hunt.providers.factory import get_job_provider
from tests.api.test_job_hunt import FakeJobProvider
from tests.fixtures.premium_helpers import expire_premium, grant_premium

EMAIL = "freeuse@example.com"
PASSWORD = "supersecret123"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": EMAIL, "password": PASSWORD})
    token = client.post(
        "/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def db(client):
    """The same session the app is using, for writing subscriptions and reading the ledger."""
    return next(app.dependency_overrides[get_db]())


@pytest.fixture()
def use_provider():
    def _use(provider):
        app.dependency_overrides[get_job_provider] = lambda: provider
        return provider

    yield _use
    app.dependency_overrides.pop(get_job_provider, None)


@pytest.fixture()
def ready(client, auth_headers, use_provider):
    """A student with a target role and a working provider -- the state a search starts from."""
    use_provider(FakeJobProvider())
    client.put(
        "/job-hunt/profile",
        json={"target_role_title": "Software Engineer"},
        headers=auth_headers,
    )
    return auth_headers


def search(client, headers, body=None, key=None):
    request_headers = dict(headers)
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return client.post("/job-hunt/search", json=body or {}, headers=request_headers)


def ledger(db):
    return db.query(JobHuntUsage).populate_existing().all()


def slot_holders(db):
    return [row for row in ledger(db) if row.free_use_slot is not None]


# -- The rule itself -------------------------------------------------------------------------


class TestTheFreeSearch:
    def test_the_first_search_is_allowed_and_returns_real_results(self, client, ready):
        response = search(client, ready)

        assert response.status_code == 200
        assert len(response.json()["results"]) == 1

    def test_the_second_search_is_refused(self, client, ready):
        assert search(client, ready).status_code == 200

        second = search(client, ready)

        assert second.status_code == 402, "402 Payment Required, not a generic 403"

    def test_the_refusal_says_what_it_is_and_what_fixes_it(self, client, ready):
        search(client, ready)

        detail = search(client, ready).json()["detail"]

        # `code` is the contract the client branches on; `message` is prose and may be
        # reworded without breaking anything.
        assert detail["code"] == "JOB_HUNT_FREE_LIMIT_REACHED"
        assert detail["requires_premium"] is True
        assert isinstance(detail["message"], str) and detail["message"]

    def test_the_refusal_is_a_different_status_from_the_premium_filter_refusal(
        self, client, ready
    ):
        """Two different 403s on one endpoint would be indistinguishable without parsing prose.

        The advanced-filter gate and the free-use gate mean different things and lead to
        different screens, so they answer with different codes and the client never has to
        read a sentence to tell them apart.
        """
        filtered = search(client, ready, {"salary_min": 500_000})
        assert filtered.status_code == 403

        assert search(client, ready).status_code == 200
        assert search(client, ready).status_code == 402

    def test_a_refused_premium_filter_does_not_cost_the_free_search(self, client, ready, db):
        """A search the server declined to run never happened."""
        assert search(client, ready, {"salary_min": 500_000}).status_code == 403

        assert slot_holders(db) == []
        assert search(client, ready).status_code == 200

    def test_opening_the_screen_costs_nothing(self, client, ready, db):
        """Reading the profile, the home summary, the tracker and the entitlement itself are
        all free. Only running a search reaches a provider, and only that is charged."""
        client.get("/job-hunt/profile", headers=ready)
        client.get("/job-hunt/home", headers=ready)
        client.get("/job-hunt/saved", headers=ready)
        client.get("/job-hunt/entitlement", headers=ready)

        assert ledger(db) == []
        assert search(client, ready).status_code == 200

    def test_editing_filters_without_searching_costs_nothing(self, client, ready, db):
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Data Analyst", "preferred_location": "Pune"},
            headers=ready,
        )

        assert ledger(db) == []


# -- Changing the request is not a new allowance ----------------------------------------------


class TestChangingTheSearchDoesNotRefillIt:
    @pytest.mark.parametrize(
        "second_body",
        [
            pytest.param({"keywords": "Data Engineer"}, id="different role"),
            pytest.param({"location": "Hyderabad"}, id="different location"),
            pytest.param({"work_mode": "remote"}, id="different work mode"),
            pytest.param({"sort": "newest"}, id="different sort"),
            pytest.param({"page": 2}, id="different page"),
            pytest.param({"page_size": 5}, id="different page size"),
            pytest.param({}, id="the identical request again"),
        ],
    )
    def test_a_second_search_is_refused_however_it_differs(self, client, ready, second_body):
        assert search(client, ready).status_code == 200

        assert search(client, ready, second_body).status_code == 402

    def test_changing_the_stored_profile_does_not_refill_it_either(self, client, ready):
        """The filters live in two places -- the request body and the saved profile -- and
        rewriting the second one is the less obvious of the two ways to ask again."""
        assert search(client, ready).status_code == 200

        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Data Engineer", "preferred_location": "Chennai"},
            headers=ready,
        )

        assert search(client, ready).status_code == 402


class TestTheSessionIsNotWhereTheLimitLives:
    def test_logging_out_and_back_in_does_not_reset_it(self, client, ready, use_provider):
        assert search(client, ready).status_code == 200

        client.post("/auth/logout", headers=ready)
        fresh = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert fresh.status_code == 200
        new_headers = {"Authorization": f"Bearer {fresh.json()['access_token']}"}
        # A fresh login is a fresh token, which is the closest a client can get to a
        # reinstall: no local state survives it, and none of it mattered anyway.
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=new_headers,
        )

        assert search(client, new_headers).status_code == 402

    def test_a_second_account_gets_its_own_search_and_cannot_reach_the_first(
        self, client, ready, use_provider, db
    ):
        """The allowance is per profile, and one student's spent search must not appear on
        another's ledger -- nor theirs on this one."""
        assert search(client, ready).status_code == 200

        client.post("/auth/signup", json={"email": "second@example.com", "password": PASSWORD})
        other = client.post(
            "/auth/login", json={"email": "second@example.com", "password": PASSWORD}
        ).json()["access_token"]
        other_headers = {"Authorization": f"Bearer {other}"}
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=other_headers,
        )

        assert search(client, other_headers).status_code == 200
        assert search(client, other_headers).status_code == 402
        assert len({row.profile_id for row in slot_holders(db)}) == 2

    def test_an_unauthenticated_request_cannot_search_at_all(self, client, ready):
        """The bypass that would make every other test here moot. There is no anonymous Job
        Hunt, so there is no ledger row to keep for one."""
        assert client.post("/job-hunt/search", json={}).status_code == 401


# -- Idempotency ------------------------------------------------------------------------------


class TestARetryIsNotASecondSearch:
    def test_the_same_key_twice_costs_one_search(self, client, ready, db):
        """The scenario this exists for: the response never reached the phone, so the phone
        asks again. Charging for that would make a dropped connection cost the free search."""
        key = f"tap-{uuid.uuid4().hex}"

        first = search(client, ready, key=key)
        second = search(client, ready, key=key)

        assert (first.status_code, second.status_code) == (200, 200)
        assert len(slot_holders(db)) == 1
        assert len(ledger(db)) == 1, "one operation, one ledger row"

    def test_a_third_search_under_a_new_key_is_still_refused(self, client, ready):
        """A replayable key must not become a way to keep searching -- only a way to finish
        the search that was already paid for."""
        key = f"tap-{uuid.uuid4().hex}"
        search(client, ready, key=key)
        search(client, ready, key=key)

        assert search(client, ready, key=f"tap-{uuid.uuid4().hex}").status_code == 402

    def test_reusing_a_key_for_a_different_search_is_a_conflict_not_a_free_ride(
        self, client, ready
    ):
        """The bypass an idempotency key would otherwise be. If the key alone re-admitted a
        request, a client could pin one key and search forever; binding it to a fingerprint of
        the parameters means the key can only ever repeat the search it was issued for."""
        key = f"tap-{uuid.uuid4().hex}"
        assert search(client, ready, key=key).status_code == 200

        conflicting = search(client, ready, {"keywords": "Data Engineer"}, key=key)

        assert conflicting.status_code == 409

    def test_a_conflict_does_not_hand_back_the_spent_search(self, client, ready, db):
        key = f"tap-{uuid.uuid4().hex}"
        search(client, ready, key=key)

        search(client, ready, {"keywords": "Data Engineer"}, key=key)

        assert len(slot_holders(db)) == 1
        assert slot_holders(db)[0].status == "succeeded"

    def test_paging_within_one_operation_reuses_the_key_and_costs_nothing_more(
        self, client, ready, db
    ):
        """A page is part of an operation, not an operation. The fingerprint deliberately
        excludes page and page_size so "load more" replays the same key without conflicting."""
        key = f"tap-{uuid.uuid4().hex}"
        assert search(client, ready, {"page": 1}, key=key).status_code == 200

        second_page = search(client, ready, {"page": 2}, key=key)

        assert second_page.status_code == 200
        assert len(ledger(db)) == 1

    @pytest.mark.parametrize(
        "bad_key",
        [
            pytest.param("short", id="too short"),
            pytest.param("k" * 65, id="too long"),
            pytest.param("has space", id="whitespace"),
            pytest.param("semi;colon", id="punctuation outside the allowed set"),
        ],
    )
    def test_a_malformed_key_is_rejected_rather_than_quietly_replaced(
        self, client, ready, bad_key
    ):
        """Substituting a generated key for a malformed one would silently stop deduplicating
        a request that expected to be deduplicated -- the failure would show up as a student
        being charged twice, far from the cause."""
        response = search(client, ready, key=bad_key)

        assert response.status_code == 400

    def test_a_malformed_key_does_not_cost_the_search(self, client, ready, db):
        search(client, ready, key="bad key")

        assert ledger(db) == []
        assert search(client, ready).status_code == 200

    def test_no_key_at_all_is_fine_and_each_request_is_its_own_operation(self, client, ready):
        """A client that sends no key is not punished and not accidentally deduplicated: it
        simply gets ordinary per-operation accounting."""
        assert search(client, ready).status_code == 200
        assert search(client, ready).status_code == 402


# -- Failures ---------------------------------------------------------------------------------


class TestAFailedSearchIsNotASpentSearch:
    @pytest.mark.parametrize(
        "error, expected_status",
        [
            pytest.param(JobProviderAuthError("401 from provider"), 503, id="provider 401"),
            pytest.param(JobProviderRateLimitError("429"), 429, id="provider 429"),
            pytest.param(JobProviderUnavailableError("502"), 502, id="provider 502"),
            pytest.param(JobProviderUnavailableError("bad json"), 502, id="malformed body"),
            pytest.param(JobProviderTimeoutError("slow"), 504, id="timeout"),
            pytest.param(JobProviderNotConfiguredError("no key"), 503, id="not configured"),
        ],
    )
    def test_a_provider_failure_leaves_the_search_available(
        self, client, ready, use_provider, db, error, expected_status
    ):
        use_provider(FakeJobProvider(error=error))

        failed = search(client, ready)

        assert failed.status_code == expected_status
        assert slot_holders(db) == [], "the allowance was handed back"
        # And it is genuinely usable, not merely unmarked.
        use_provider(FakeJobProvider())
        assert search(client, ready).status_code == 200

    def test_the_failed_attempt_is_still_recorded(self, client, ready, use_provider, db):
        """Released, not erased. The attempt happened, and a ledger that forgot it could not
        answer "why does this student say their search vanished"."""
        use_provider(FakeJobProvider(error=JobProviderUnavailableError("down")))

        search(client, ready)

        rows = ledger(db)
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].free_use_slot is None
        assert rows[0].consumed_at is None

    def test_an_unexpected_internal_failure_also_leaves_it_available(
        self, client, ready, monkeypatch, db
    ):
        """Not just provider errors. Anything that stops a result being produced returns the
        allowance, because the student got nothing either way."""
        from app.services.job_hunt import service as job_hunt_service

        def explode(*_args, **_kwargs):
            raise RuntimeError("something internal broke")

        monkeypatch.setattr(job_hunt_service, "saved_jobs_for_listings", explode)

        assert search(client, ready).status_code == 502
        assert slot_holders(db) == []

    def test_an_incomplete_profile_does_not_cost_the_search(self, client, auth_headers, use_provider, db):
        """422 means we never looked. Charging for it would spend the search on a validation
        error the student can fix in five seconds."""
        use_provider(FakeJobProvider())

        assert search(client, auth_headers).status_code == 422
        assert slot_holders(db) == []

    def test_repeated_failures_cannot_be_farmed_into_extra_successes(
        self, client, ready, use_provider, db
    ):
        """The refund is not an exploit: it returns the allowance to exactly where it was, so
        any number of failures still leaves one search, never two."""
        for _ in range(5):
            use_provider(FakeJobProvider(error=JobProviderUnavailableError("down")))
            assert search(client, ready).status_code == 502

        use_provider(FakeJobProvider())
        assert search(client, ready).status_code == 200
        assert search(client, ready).status_code == 402
        assert len(slot_holders(db)) == 1

    def test_a_failure_after_a_success_under_the_same_key_does_not_refund_the_success(
        self, client, ready, use_provider, db
    ):
        """The refund path is conditional on the row still being unsettled, and this is why.
        Search, get results, then replay the key into a failure -- the search stays spent."""
        key = f"tap-{uuid.uuid4().hex}"
        assert search(client, ready, key=key).status_code == 200

        use_provider(FakeJobProvider(error=JobProviderUnavailableError("down")))
        assert search(client, ready, key=key).status_code == 502

        assert len(slot_holders(db)) == 1
        assert slot_holders(db)[0].status == "succeeded"
        use_provider(FakeJobProvider())
        assert search(client, ready, key=f"tap-{uuid.uuid4().hex}").status_code == 402


# -- Saved searches -----------------------------------------------------------------------------


class TestTheSavedSearchDoorIsTheSameDoor:
    def _saved_search_id(self, client, headers):
        return client.post(
            "/job-hunt/saved-searches",
            json={"name": "Bengaluru engineers", "filters": {"keywords": "engineer"}},
            headers=headers,
        ).json()["id"]

    def test_storing_a_saved_search_costs_nothing(self, client, ready, db):
        self._saved_search_id(client, ready)

        assert ledger(db) == []

    def test_running_a_saved_search_spends_the_same_allowance_as_a_search(self, client, ready):
        search_id = self._saved_search_id(client, ready)

        assert client.post(
            f"/job-hunt/saved-searches/{search_id}/run", headers=ready
        ).status_code == 200
        assert search(client, ready).status_code == 402

    def test_a_saved_search_cannot_be_run_twice_on_the_free_tier(self, client, ready):
        """The two-request bypass this gate exists to close: store a search once, then run it
        as often as you like."""
        search_id = self._saved_search_id(client, ready)
        assert client.post(
            f"/job-hunt/saved-searches/{search_id}/run", headers=ready
        ).status_code == 200

        second = client.post(f"/job-hunt/saved-searches/{search_id}/run", headers=ready)

        assert second.status_code == 402
        assert second.json()["detail"]["code"] == "JOB_HUNT_FREE_LIMIT_REACHED"

    def test_a_second_saved_search_is_not_a_second_allowance(self, client, ready):
        first = self._saved_search_id(client, ready)
        second = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Pune engineers", "filters": {"keywords": "engineer", "location": "Pune"}},
            headers=ready,
        ).json()["id"]

        assert client.post(f"/job-hunt/saved-searches/{first}/run", headers=ready).status_code == 200
        assert client.post(f"/job-hunt/saved-searches/{second}/run", headers=ready).status_code == 402

    def test_a_failed_saved_search_run_does_not_cost_the_search(
        self, client, ready, use_provider, db
    ):
        search_id = self._saved_search_id(client, ready)
        use_provider(FakeJobProvider(error=JobProviderUnavailableError("down")))

        assert client.post(
            f"/job-hunt/saved-searches/{search_id}/run", headers=ready
        ).status_code == 502
        assert slot_holders(db) == []

    def test_running_someone_elses_saved_search_is_a_404_and_costs_nothing(
        self, client, ready, db
    ):
        client.post("/auth/signup", json={"email": "third@example.com", "password": PASSWORD})
        other = client.post(
            "/auth/login", json={"email": "third@example.com", "password": PASSWORD}
        ).json()["access_token"]
        other_headers = {"Authorization": f"Bearer {other}"}
        theirs = self._saved_search_id(client, other_headers)

        # 404 rather than 403: a 403 would confirm the id exists.
        assert client.post(
            f"/job-hunt/saved-searches/{theirs}/run", headers=ready
        ).status_code == 404
        assert slot_holders(db) == []


# -- Premium ------------------------------------------------------------------------------------


class TestASubscriptionLiftsTheCeiling:
    def test_a_subscriber_can_search_repeatedly(self, client, ready, db):
        grant_premium(db, EMAIL)

        for _ in range(5):
            assert search(client, ready).status_code == 200

    def test_subscribing_after_the_free_search_unblocks_it_immediately(self, client, ready, db):
        assert search(client, ready).status_code == 200
        assert search(client, ready).status_code == 402

        grant_premium(db, EMAIL)

        assert search(client, ready).status_code == 200

    def test_a_subscribers_searches_do_not_spend_the_free_one(self, client, ready, db):
        """A subscription is unlimited use, not an early spend of the free search. A student
        who subscribes first and lapses later still has the search they never used."""
        grant_premium(db, EMAIL)
        for _ in range(3):
            assert search(client, ready).status_code == 200

        assert slot_holders(db) == []

        expire_premium(db, EMAIL)
        assert search(client, ready).status_code == 200
        assert search(client, ready).status_code == 402

    def test_an_expired_subscription_returns_to_the_free_rules(self, client, ready, db):
        assert search(client, ready).status_code == 200
        grant_premium(db, EMAIL)
        assert search(client, ready).status_code == 200

        expire_premium(db, EMAIL)

        assert search(client, ready).status_code == 402

    def test_the_free_use_record_survives_the_subscription(self, client, ready, db):
        """Explicitly required by the product rule: premium must not erase the history, or a
        month's subscription would buy a permanent second free search on every lapse."""
        search(client, ready)
        holder = slot_holders(db)[0]
        spent_at = holder.consumed_at

        grant_premium(db, EMAIL)
        search(client, ready)
        expire_premium(db, EMAIL)

        still_there = slot_holders(db)
        assert len(still_there) == 1
        assert still_there[0].id == holder.id
        assert still_there[0].consumed_at == spent_at

    def test_a_subscribers_usage_is_still_recorded(self, client, ready, db):
        """The ledger records what happened, not only what was rationed -- otherwise there is
        no audit trail for the searches that cost the deployment its provider quota."""
        grant_premium(db, EMAIL)
        search(client, ready)

        rows = ledger(db)
        assert len(rows) == 1
        assert rows[0].was_premium is True
        assert rows[0].free_use_slot is None
        assert rows[0].status == "succeeded"

    def test_a_subscriber_may_run_a_saved_search_repeatedly(self, client, ready, db):
        grant_premium(db, EMAIL)
        search_id = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Engineers", "filters": {"keywords": "engineer"}},
            headers=ready,
        ).json()["id"]

        for _ in range(3):
            assert client.post(
                f"/job-hunt/saved-searches/{search_id}/run", headers=ready
            ).status_code == 200


# -- What the client is told ---------------------------------------------------------------------


class TestTheEntitlementEndpoint:
    def test_a_new_student_is_told_they_have_one_free_search(self, client, ready):
        body = client.get("/job-hunt/entitlement", headers=ready).json()

        assert body["tier"] == "free"
        assert body["free_uses_remaining"] == 1
        assert body["has_used_free_search"] is False
        assert body["requires_premium"] is False
        assert body["last_free_use_at"] is None

    def test_after_the_free_search_it_says_so_and_names_the_fix(self, client, ready):
        search(client, ready)

        body = client.get("/job-hunt/entitlement", headers=ready).json()

        assert body["free_uses_remaining"] == 0
        assert body["has_used_free_search"] is True
        assert body["requires_premium"] is True
        assert body["last_free_use_at"] is not None

    def test_a_subscriber_is_never_told_they_need_premium(self, client, ready, db):
        search(client, ready)
        grant_premium(db, EMAIL)

        body = client.get("/job-hunt/entitlement", headers=ready).json()

        assert body["tier"] == "premium"
        assert body["unlimited"] is True
        # They have used it, but that is not a reason to show them an upgrade prompt.
        assert body["has_used_free_search"] is True
        assert body["requires_premium"] is False

    def test_reading_it_never_consumes_anything(self, client, ready, db):
        for _ in range(3):
            client.get("/job-hunt/entitlement", headers=ready)

        assert ledger(db) == []

    def test_it_requires_authentication(self, client):
        assert client.get("/job-hunt/entitlement").status_code == 401


# -- The ledger itself -----------------------------------------------------------------------------


class TestTheLedgerRow:
    def test_a_spent_search_is_marked_succeeded_and_timestamped(self, client, ready, db):
        search(client, ready)

        row = slot_holders(db)[0]
        assert row.status == "succeeded"
        assert row.usage_type == "search"
        assert row.free_use_slot == 1
        assert row.was_premium is False
        assert isinstance(row.consumed_at, datetime.datetime)

    def test_a_saved_search_run_is_recorded_under_its_own_type(self, client, ready, db):
        search_id = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Engineers", "filters": {"keywords": "engineer"}},
            headers=ready,
        ).json()["id"]
        client.post(f"/job-hunt/saved-searches/{search_id}/run", headers=ready)

        assert slot_holders(db)[0].usage_type == "saved_search_run"

    def test_deleting_the_account_removes_the_ledger_rows(self, client, ready, db):
        """Every other user-owned table is erased on account deletion and this one is no
        different. It is not a way around the limit: the only route here is the owner deleting
        their whole account, resumes and interviews included."""
        search(client, ready)
        assert len(ledger(db)) == 1

        deleted = client.request(
            "DELETE", "/users/me", json={"password": PASSWORD}, headers=ready
        )

        assert deleted.status_code == 204, deleted.text
        assert ledger(db) == []
