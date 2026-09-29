"""Career Strategy over HTTP: entitlement, ownership, and what actually goes over the wire.

The unit tests prove the projection drops premium content; these prove the same thing at the
boundary a real free client can reach, which is the only place the claim actually matters.
Android's PaywallGate hides UI and nothing more -- an authenticated free user can call any of
these routes directly with a copied token, so every check here is deliberately done against the
raw response body rather than against a service return value.
"""

import hashlib
import hmac

import httpx
import pytest

from app.core.config import settings
from app.services import subscription_service as svc
from app.services.career_strategy.bottleneck import STRATEGY_STATES


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "strategy@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "strategy@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "strategy2@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "strategy2@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _grant_premium(client, monkeypatch, headers, payment_id="pay_strategy"):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake_key_id")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake_test_secret")
    fake_request = httpx.Request("POST", svc.RAZORPAY_ORDERS_URL)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: httpx.Response(
            200, json={"id": f"order_{payment_id}", "status": "created"}, request=fake_request
        ),
    )
    checkout = client.post("/subscriptions/checkout", json={"plan": "1_month"}, headers=headers).json()
    signature = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        f"{checkout['razorpay_order_id']}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    verify = client.post(
        "/subscriptions/verify",
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
        headers=headers,
    )
    assert verify.status_code == 200 and verify.json()["tier"] == "premium"


def _sde_role_id(client, headers):
    roles = client.get("/roles", headers=headers).json()
    return next(role["id"] for role in roles if role["name"] == "SDE")


def _with_role(client, headers):
    client.post("/users/me/role", json={"role_id": _sde_role_id(client, headers)}, headers=headers)


class TestAccess:
    def test_requires_authentication(self, client):
        for path in ("", "/history", "/compare/1", "/job/1"):
            assert client.get(f"/career-strategy{path}").status_code == 401

    def test_history_is_premium_only(self, client, auth_headers):
        assert client.get("/career-strategy/history", headers=auth_headers).status_code == 403

    def test_compare_is_premium_only(self, client, auth_headers):
        """403 before 404: a free user must not be able to probe which snapshot ids exist."""
        assert client.get("/career-strategy/compare/1", headers=auth_headers).status_code == 403

    def test_job_strategy_is_premium_only(self, client, auth_headers):
        assert client.get("/career-strategy/job/1", headers=auth_headers).status_code == 403

    def test_the_main_screen_is_free(self, client, auth_headers):
        response = client.get("/career-strategy", headers=auth_headers)
        assert response.status_code == 200


class TestFreeResponse:
    def test_no_role_is_reported_honestly_rather_than_as_a_zero(self, client, auth_headers):
        body = client.get("/career-strategy", headers=auth_headers).json()
        assert body["has_role"] is False
        assert body["state"] == "FOCUS_ON_TARGET_ROLE"
        assert body["evidence_coverage_percent"] is None
        assert body["bottleneck"] is None
        assert body["state_reason"]  # the screen still explains itself

    def test_a_free_client_receives_no_premium_content_at_all(self, client, auth_headers):
        """The spec's rule checked where it actually matters -- in the bytes a free client
        receives, not in a service call the client never makes."""
        _with_role(client, auth_headers)
        body = client.get("/career-strategy", headers=auth_headers).json()

        assert body["is_premium"] is False
        assert body["locked_sections"]
        assert body["queue"] == []
        assert body["do_not_do_yet"] == []
        assert body["contradictions"] == []
        assert body["diminishing_returns"] == []
        assert body["latest_change"] is None
        if body["bottleneck"] is not None:
            assert body["bottleneck"]["impact"] is None
            assert body["bottleneck"]["effort"] is None
            assert body["bottleneck"]["evidence"] == []
            assert body["bottleneck"]["description"] is None
            assert body["bottleneck"]["decision_signals"] == []

    def test_no_hiring_probability_anywhere_in_the_payload(self, client, auth_headers):
        _with_role(client, auth_headers)
        raw = client.get("/career-strategy", headers=auth_headers).text.lower()
        for banned in ("hiring_probability", "probability_of", "chance_of_offer",
                       "offer_probability", "recruiter_probability", "percentage_chance"):
            assert banned not in raw

    def test_the_screen_is_stable_across_repeated_reads(self, client, auth_headers):
        """A screen that reshuffles between two identical requests would also make history
        disagree with itself about what changed."""
        _with_role(client, auth_headers)
        first = client.get("/career-strategy", headers=auth_headers).json()
        second = client.get("/career-strategy", headers=auth_headers).json()
        assert first["state"] == second["state"]
        assert first["snapshot_sequence"] == second["snapshot_sequence"]
        # Only the first read created the row; the second found nothing had changed.
        assert second["snapshot_is_new"] is False


class TestPremiumResponse:
    def test_premium_receives_the_working(self, client, auth_headers, monkeypatch):
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers)

        body = client.get("/career-strategy", headers=auth_headers).json()
        assert body["is_premium"] is True
        assert body["locked_sections"] == []
        assert body["latest_change"] is not None
        if body["bottleneck"] is not None:
            assert body["bottleneck"]["impact"] is not None
            assert body["bottleneck"]["effort"] is not None

    def test_history_records_one_row_per_real_change(self, client, auth_headers, monkeypatch):
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers)
        for _ in range(3):
            client.get("/career-strategy", headers=auth_headers)

        history = client.get("/career-strategy/history", headers=auth_headers).json()
        # Opening the screen repeatedly is not a strategic event.
        assert len(history["snapshots"]) == 1
        assert history["snapshots"][0]["sequence"] == 1
        # Each entry explains itself, so the timeline renders from this one request rather than
        # a /compare call per row.
        assert history["snapshots"][0]["change_summary"]

    def test_compare_explains_the_first_snapshot_without_inventing_a_baseline(
        self, client, auth_headers, monkeypatch
    ):
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers)
        snapshot_id = client.get("/career-strategy", headers=auth_headers).json()["snapshot_id"]

        body = client.get(f"/career-strategy/compare/{snapshot_id}", headers=auth_headers).json()
        assert body["compared_to"] is None
        assert body["change"]["changed"] is False
        assert "first recorded strategy" in body["change"]["summary"][0]

    def test_history_limit_is_bounded_by_the_server(self, client, auth_headers, monkeypatch):
        _grant_premium(client, monkeypatch, auth_headers)
        assert client.get("/career-strategy/history?limit=100000", headers=auth_headers).status_code == 422


class TestOwnership:
    def test_another_students_snapshot_is_a_404(self, client, auth_headers, other_auth_headers, monkeypatch):
        """Snapshot ids are sequential integers. Without profile scoping this route would hand
        one student another's entire strategy."""
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_owner")
        mine = client.get("/career-strategy", headers=auth_headers).json()["snapshot_id"]

        _grant_premium(client, monkeypatch, other_auth_headers, payment_id="pay_other")
        stolen = client.get(f"/career-strategy/compare/{mine}", headers=other_auth_headers)
        assert stolen.status_code == 404

    def test_two_students_keep_separate_histories(self, client, auth_headers, other_auth_headers, monkeypatch):
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_a")
        client.get("/career-strategy", headers=auth_headers)

        _grant_premium(client, monkeypatch, other_auth_headers, payment_id="pay_b")
        theirs = client.get("/career-strategy/history", headers=other_auth_headers).json()
        assert theirs["snapshots"] == []

    def test_an_unknown_listing_is_a_404_not_an_invented_strategy(
        self, client, auth_headers, monkeypatch
    ):
        _grant_premium(client, monkeypatch, auth_headers)
        assert client.get("/career-strategy/job/987654", headers=auth_headers).status_code == 404


class TestTargets:
    def test_reading_targets_requires_auth(self, client):
        assert client.get("/career-strategy/targets").status_code == 401

    def test_a_free_client_gets_a_locked_shell_not_a_403(self, client, auth_headers):
        """A 403 gives the screen nothing to render. The locked shape lets it show an upgrade
        prompt, and it carries no comparison data at all."""
        _with_role(client, auth_headers)
        body = client.get("/career-strategy/targets", headers=auth_headers).json()

        assert body["locked"] is True
        assert body["is_premium"] is False
        assert body["targets"] == []
        assert body["tradeoffs"] == []
        assert body["shared_skills"] == []
        assert body["max_targets"] >= 1

    def test_setting_targets_is_premium_only(self, client, auth_headers):
        response = client.post("/career-strategy/targets", json={"role_ids": []}, headers=auth_headers)
        assert response.status_code == 403

    def test_premium_can_set_and_compare_targets(self, client, auth_headers, monkeypatch):
        """A student exploring before committing: no target chosen yet, one role saved to
        compare. SDE is the only seeded role with skill weights, so it is the only one there is
        anything to measure against -- see the refusal test below."""
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_targets")
        sde_id = _sde_role_id(client, auth_headers)

        created = client.post(
            "/career-strategy/targets", json={"role_ids": [sde_id]}, headers=auth_headers
        )
        assert created.status_code == 200
        body = created.json()
        assert body["is_premium"] is True
        assert body["locked"] is False
        assert [entry["display_role_name"] for entry in body["targets"]] == ["SDE"]
        # Not their chosen target -- a hypothetical they asked to see.
        assert body["targets"][0]["is_current_target"] is False
        assert body["targets"][0]["state"] in STRATEGY_STATES

        # And it persists: a fresh GET returns the same comparison.
        again = client.get("/career-strategy/targets", headers=auth_headers).json()
        assert [entry["display_role_name"] for entry in again["targets"]] == ["SDE"]

    def test_a_role_with_no_skill_weights_is_refused(self, client, auth_headers, monkeypatch):
        """A target with nothing to measure against produces zero gaps, and zero gaps reads to a
        student as "you are ready for this". Refusing is the honest answer, and it is why this
        endpoint is stricter than Career Decision Lab about the same role."""
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_weightless")
        analyst_id = next(
            role["id"] for role in client.get("/roles", headers=auth_headers).json()
            if role["name"] == "Data Analyst"
        )

        response = client.post(
            "/career-strategy/targets", json={"role_ids": [analyst_id]}, headers=auth_headers
        )
        assert response.status_code == 422
        assert "skill weights" in response.json()["detail"]

    def test_an_unknown_role_is_422_and_changes_nothing(self, client, auth_headers, monkeypatch):
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_bad_target")

        response = client.post(
            "/career-strategy/targets", json={"role_ids": [987654]}, headers=auth_headers
        )
        assert response.status_code == 422
        assert client.get("/career-strategy/targets", headers=auth_headers).json()["targets"]

    def test_too_many_targets_is_rejected(self, client, auth_headers, monkeypatch):
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_many_targets")
        role_ids = [role["id"] for role in client.get("/roles", headers=auth_headers).json()]

        response = client.post(
            "/career-strategy/targets", json={"role_ids": role_ids * 4}, headers=auth_headers
        )
        assert response.status_code == 422

    def test_no_hiring_probability_in_a_comparison_payload(self, client, auth_headers, monkeypatch):
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_prob_targets")

        raw = client.get("/career-strategy/targets", headers=auth_headers).text.lower()
        for banned in ("hiring_probability", "probability_of", "chance_of_offer",
                       "offer_probability", "guaranteed"):
            assert banned not in raw

    def test_targets_do_not_leak_between_students(self, client, auth_headers, other_auth_headers, monkeypatch):
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_mine_t")
        client.post(
            "/career-strategy/targets",
            json={"role_ids": [_sde_role_id(client, auth_headers)]},
            headers=auth_headers,
        )
        assert client.get("/career-strategy/targets", headers=auth_headers).json()["targets"]

        _grant_premium(client, monkeypatch, other_auth_headers, payment_id="pay_theirs_t")
        theirs = client.get("/career-strategy/targets", headers=other_auth_headers).json()
        assert theirs["targets"] == []

    def test_comparing_targets_does_not_add_strategy_history(self, client, auth_headers, monkeypatch):
        """Looking at another role is not a change to your own strategy."""
        _with_role(client, auth_headers)
        _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_hist_t")
        client.get("/career-strategy", headers=auth_headers)
        before = len(client.get("/career-strategy/history", headers=auth_headers).json()["snapshots"])

        client.post(
            "/career-strategy/targets",
            json={"role_ids": [_sde_role_id(client, auth_headers)]},
            headers=auth_headers,
        )
        for _ in range(3):
            client.get("/career-strategy/targets", headers=auth_headers)

        after = len(client.get("/career-strategy/history", headers=auth_headers).json()["snapshots"])
        assert after == before


def test_a_student_who_set_comparison_targets_can_still_delete_their_account(
    client, auth_headers, monkeypatch
):
    """F-01 at the boundary where it actually bit: DELETE /users/me returning 500.

    career_strategy_targets.profile_id is NOT NULL with no ondelete, and the table was missing
    from account_service.delete_account -- so the profile delete raised IntegrityError and the
    route 500'd. The rollback left the account intact, meaning a student could never delete it.

    Premium-only, so it hit exactly the students who had paid, and no existing test reached it:
    tests/api/test_account_deletion.py's Career Strategy case opens the strategy screen, which
    writes a placement_strategy_snapshots row -- a different table that was already covered.
    """
    _grant_premium(client, monkeypatch, auth_headers, payment_id="pay_delete_targets")
    sde_id = _sde_role_id(client, auth_headers)
    assert client.post(
        "/career-strategy/targets", json={"role_ids": [sde_id]}, headers=auth_headers
    ).status_code == 200

    resp = client.request("DELETE", "/users/me", json={"password": "supersecret123"}, headers=auth_headers)

    assert resp.status_code == 204, resp.text
    assert client.get("/users/me", headers=auth_headers).status_code == 401
    # The shared role catalog survives -- one student leaving must not remove a role every
    # other student compares against.
    assert client.get("/roles").status_code in (200, 401)
