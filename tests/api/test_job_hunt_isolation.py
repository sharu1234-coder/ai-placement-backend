"""Cross-account isolation and account-deletion coverage for Job Hunt.

Job Hunt has a wrinkle none of this app's other features has, and it is the reason this file
exists separately: job_listings is *deliberately shared*. A listing is public provider content
that many students can match against, so it carries no profile_id and is not deleted with an
account. Everything personal -- the score, the saved row, the pipeline status, the notes -- is
keyed to a profile.

That split has to be tested from both sides, because getting it wrong in either direction is
serious. Too little sharing and deleting one account destroys another student's saved jobs; too
much and one student can read or mutate another's application pipeline. So the tests below
assert both that private rows are unreachable across accounts, and that the shared listing
survives a deletion while every private row keyed to it does not.

The convention asserted is the codebase's own: a row owned by someone else is indistinguishable
from one that does not exist, so 404, never 403.
"""

import pytest

from app.api.deps import get_db
from app.main import app
from app.services.job_hunt.providers.factory import get_job_provider
from tests.api.test_job_hunt import FakeJobProvider, provider_job


@pytest.fixture()
def use_provider():
    def _use(provider):
        app.dependency_overrides[get_job_provider] = lambda: provider
        return provider

    yield _use
    app.dependency_overrides.pop(get_job_provider, None)


def signup(client, email: str) -> dict:
    client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def alice(client):
    return signup(client, "alice@example.com")


@pytest.fixture()
def bob(client):
    return signup(client, "bob@example.com")


def seed_saved_job(client, headers, use_provider) -> tuple:
    """Give one student a listing, a computed match and a saved row. Returns (listing_id,
    saved_job_id)."""
    use_provider(FakeJobProvider(jobs=[provider_job()]))
    client.put("/job-hunt/profile", json={"target_role_title": "Software Engineer"}, headers=headers)
    listing_id = client.post("/job-hunt/search", json={}, headers=headers).json()["results"][0]["listing"]["id"]
    saved_id = client.post("/job-hunt/saved", json={"listing_id": listing_id}, headers=headers).json()["id"]
    return listing_id, saved_id


def _assert_denied(response, route: str) -> None:
    """404, not 403: an id owned by someone else must be indistinguishable from a missing one,
    or an attacker can confirm which ids are real by probing."""
    assert response.status_code == 404, (
        f"{route} answered {response.status_code}, expected 404 -- a 403 confirms the row exists"
    )


class TestSavedJobIsolation:
    def test_another_student_cannot_read_your_tracker(self, client, alice, bob, use_provider):
        seed_saved_job(client, alice, use_provider)
        assert client.get("/job-hunt/saved", headers=bob).json() == []

    def test_another_student_cannot_change_your_application_status(self, client, alice, bob, use_provider):
        _listing_id, saved_id = seed_saved_job(client, alice, use_provider)
        _assert_denied(
            client.patch(f"/job-hunt/saved/{saved_id}", json={"status": "rejected"}, headers=bob),
            f"PATCH /job-hunt/saved/{saved_id}",
        )
        # And the owner's row is genuinely untouched, not merely un-returned.
        assert client.get("/job-hunt/saved", headers=alice).json()[0]["status"] == "saved"

    def test_another_student_cannot_read_your_private_notes(self, client, alice, bob, use_provider):
        _listing_id, saved_id = seed_saved_job(client, alice, use_provider)
        client.patch(
            f"/job-hunt/saved/{saved_id}", json={"notes": "Salary expectation 12 LPA"}, headers=alice
        )
        assert "12 LPA" not in client.get("/job-hunt/saved", headers=bob).text

    def test_another_student_cannot_delete_your_saved_job(self, client, alice, bob, use_provider):
        _listing_id, saved_id = seed_saved_job(client, alice, use_provider)
        _assert_denied(
            client.delete(f"/job-hunt/saved/{saved_id}", headers=bob),
            f"DELETE /job-hunt/saved/{saved_id}",
        )
        assert len(client.get("/job-hunt/saved", headers=alice).json()) == 1


class TestProfileIsolation:
    def test_each_student_gets_their_own_job_hunt_profile(self, client, alice, bob):
        client.put("/job-hunt/profile", json={"target_role_title": "Data Analyst"}, headers=alice)
        assert client.get("/job-hunt/profile", headers=bob).json()["target_role_title"] is None

    def test_one_students_preferences_never_leak_into_anothers(self, client, alice, bob):
        client.put(
            "/job-hunt/profile",
            json={"preferred_location": "Bengaluru", "salary_min_expected": 1200000},
            headers=alice,
        )
        body = client.get("/job-hunt/profile", headers=bob).json()
        assert body["preferred_location"] is None
        assert body["salary_min_expected"] is None


class TestSavedSearchIsolation:
    def test_saved_searches_are_private(self, client, alice, bob):
        client.post(
            "/job-hunt/saved-searches",
            json={"name": "Alice's remote roles", "filters": {"keywords": "sde"}},
            headers=alice,
        )
        assert client.get("/job-hunt/saved-searches", headers=bob).json() == []

    def test_another_student_cannot_delete_your_saved_search(self, client, alice, bob):
        search_id = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Mine", "filters": {"keywords": "sde"}},
            headers=alice,
        ).json()["id"]
        _assert_denied(
            client.delete(f"/job-hunt/saved-searches/{search_id}", headers=bob),
            f"DELETE /job-hunt/saved-searches/{search_id}",
        )
        assert len(client.get("/job-hunt/saved-searches", headers=alice).json()) == 1


class TestSavedJobProvenanceIsolation:
    """`source_saved_search_id` is a cross-user id supplied in a request body.

    Every *path* id in Job Hunt is scoped to the caller's profile, but this one arrives in the
    body of POST /job-hunt/saved and was stored unchecked, then resolved for display with a
    bare primary-key lookup. Two unscoped steps in a row: a student could tag their own saved
    job with another student's saved-search id and read that student's saved-search *name*
    back out of their own tracker. Saved-search names are user-authored text -- "Dubai Data
    Analyst - Priya referral" is a plausible one.
    """

    def test_tagging_a_saved_job_with_another_students_search_is_refused(
        self, client, alice, bob, use_provider
    ):
        bob_search_id = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Bob's private search", "filters": {"keywords": "data"}},
            headers=bob,
        ).json()["id"]
        listing_id, _saved = seed_saved_job(client, alice, use_provider)

        response = client.post(
            "/job-hunt/saved",
            json={
                "listing_id": listing_id,
                "source": "saved_search",
                "source_saved_search_id": bob_search_id,
            },
            headers=alice,
        )

        _assert_denied(response, "POST /job-hunt/saved with another student's saved_search_id")

    def test_the_tracker_never_names_another_students_saved_search(
        self, client, alice, bob, use_provider
    ):
        """Defence in depth, and the part that matters for rows written before the check
        above existed: even holding a foreign id, the read path must not resolve its name."""
        bob_search_id = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Bob's private search", "filters": {"keywords": "data"}},
            headers=bob,
        ).json()["id"]
        listing_id, saved_id = seed_saved_job(client, alice, use_provider)

        # Written straight to the database, bypassing the API check, to stand for a row saved
        # before it was added.
        from app.models.saved_job import SavedJob

        session = next(app.dependency_overrides[get_db]())
        row = session.get(SavedJob, saved_id)
        row.source = "saved_search"
        row.source_saved_search_id = bob_search_id
        session.commit()

        listed = client.get("/job-hunt/saved", headers=alice).json()
        assert listed[0]["source_saved_search_name"] is None, (
            "another student's saved-search name must never be resolved for display"
        )

        detail = client.patch(
            f"/job-hunt/saved/{saved_id}", json={"notes": "x"}, headers=alice
        ).json()
        assert detail["source_saved_search_name"] is None

    def test_a_students_own_saved_search_is_still_named(self, client, alice, use_provider):
        """The guard against over-correcting: legitimate provenance must survive.

        Saved once, not twice -- save_job is idempotent and deliberately never rewrites the
        source of an existing row, so seeding a saved job first would leave it "manual".
        """
        search_id = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Dubai Data Analyst", "filters": {"keywords": "data"}},
            headers=alice,
        ).json()["id"]
        use_provider(FakeJobProvider(jobs=[provider_job()]))
        client.put(
            "/job-hunt/profile",
            json={"target_role_title": "Software Engineer"},
            headers=alice,
        )
        listing_id = client.post("/job-hunt/search", json={}, headers=alice).json()[
            "results"
        ][0]["listing"]["id"]

        saved = client.post(
            "/job-hunt/saved",
            json={
                "listing_id": listing_id,
                "source": "saved_search",
                "source_saved_search_id": search_id,
            },
            headers=alice,
        ).json()

        assert saved["source_saved_search_name"] == "Dubai Data Analyst"


class TestMatchIsolation:
    def test_the_shared_listing_is_readable_but_each_student_gets_their_own_match(
        self, client, alice, bob, use_provider
    ):
        """The deliberate half of the sharing split. The advert itself holds nothing private,
        so Bob may read it -- but the score he sees is computed for *him*, from his own
        profile, never Alice's."""
        listing_id, _saved_id = seed_saved_job(client, alice, use_provider)

        client.put("/job-hunt/profile", json={"target_role_title": "Data Analyst"}, headers=bob)
        bob_view = client.get(f"/job-hunt/jobs/{listing_id}", headers=bob).json()
        alice_view = client.get(f"/job-hunt/jobs/{listing_id}", headers=alice).json()

        assert bob_view["listing"]["id"] == alice_view["listing"]["id"] == listing_id
        # Alice targets the role this advert is for; Bob does not.
        assert alice_view["match"]["total_score"] > bob_view["match"]["total_score"]
        # And Bob must not see Alice's saved state on the shared listing.
        assert bob_view["saved_job_id"] is None
        assert alice_view["saved_job_id"] is not None


class TestAccountDeletion:
    def test_deleting_an_account_removes_every_private_job_hunt_row(self, client, alice, use_provider):
        seed_saved_job(client, alice, use_provider)
        client.post(
            "/job-hunt/saved-searches",
            json={"name": "Mine", "filters": {"keywords": "sde"}},
            headers=alice,
        )
        client.put("/job-hunt/profile", json={"preferred_location": "Pune"}, headers=alice)

        # Deletion requires the current password -- a stolen access token alone must not be
        # able to trigger an irreversible wipe (see app/api/users.py::delete_my_account).
        resp = client.request(
            "DELETE", "/users/me", json={"password": "supersecret123"}, headers=alice
        )
        assert resp.status_code == 204, resp.text

        # The account is gone, so its own token no longer resolves to anything.
        assert client.get("/job-hunt/saved", headers=alice).status_code == 401

    def test_deleting_one_account_does_not_destroy_another_students_saved_jobs(
        self, client, alice, bob, use_provider
    ):
        """The failure this guards against is specific to Job Hunt's shared-listing design: if
        account deletion also deleted job_listings rows, removing Alice would silently empty
        Bob's tracker, because both saved the same advert."""
        listing_id, _alice_saved = seed_saved_job(client, alice, use_provider)
        bob_saved = client.post(
            "/job-hunt/saved", json={"listing_id": listing_id}, headers=bob
        )
        assert bob_saved.status_code == 201

        deleted = client.request(
            "DELETE", "/users/me", json={"password": "supersecret123"}, headers=alice
        )
        assert deleted.status_code == 204, deleted.text

        bob_tracker = client.get("/job-hunt/saved", headers=bob).json()
        assert len(bob_tracker) == 1
        assert bob_tracker[0]["listing"]["id"] == listing_id
        # And the listing is still fully readable, not a dangling reference.
        assert client.get(f"/job-hunt/jobs/{listing_id}", headers=bob).status_code == 200

    def test_deleting_an_account_removes_alerts_and_their_delivery_history(
        self, client, alice, use_provider
    ):
        """Alerts and their delivery rows are per-student data, and both reference tables that
        outlive the student -- job_saved_searches and job_listings. Getting the delete order
        wrong here fails on a real foreign key rather than leaving orphans quietly behind, so
        this exercises the whole chain in one go: search -> saved search -> alert -> delete."""
        seed_saved_job(client, alice, use_provider)
        search_id = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Watched", "filters": {"keywords": "sde"}},
            headers=alice,
        ).json()["id"]
        alert = client.post(
            "/job-hunt/alerts",
            json={"name": "Watched", "saved_search_id": search_id, "is_enabled": True},
            headers=alice,
        )
        assert alert.status_code == 201

        resp = client.request(
            "DELETE", "/users/me", json={"password": "supersecret123"}, headers=alice
        )
        assert resp.status_code == 204, resp.text
        assert client.get("/job-hunt/alerts", headers=alice).status_code == 401

    def test_deleting_an_account_removes_a_job_saved_with_search_provenance(
        self, client, alice, use_provider
    ):
        """saved_jobs gained a foreign key to job_saved_searches for tracker provenance, which
        means saved_jobs now has to be deleted before saved searches. This is the test that
        fails if that order is ever reversed."""
        listing_id, _saved_id = seed_saved_job(client, alice, use_provider)
        search_id = client.post(
            "/job-hunt/saved-searches",
            json={"name": "Source", "filters": {"keywords": "sde"}},
            headers=alice,
        ).json()["id"]
        client.post(
            "/job-hunt/saved",
            json={
                "listing_id": listing_id,
                "source": "saved_search",
                "source_saved_search_id": search_id,
            },
            headers=alice,
        )

        resp = client.request(
            "DELETE", "/users/me", json={"password": "supersecret123"}, headers=alice
        )
        assert resp.status_code == 204, resp.text
