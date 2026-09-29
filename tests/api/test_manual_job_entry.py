"""Entering a job by hand, and everything downstream of it.

Before Phase 4 a JobListing could only be created by provider ingestion, so the entire outcome
loop -- listing, saved job, career goal, applied, interview, offer -- was gated on a paid
search against a quota shared by every user of the deployment. A student whose jobs came from
campus placement, a referral or a WhatsApp message could not use the tracker at all.

Two things are proven here that nothing else in the suite can prove.

*That the loop runs with no provider.* The provider dependency is replaced with one that fails
the test if it is called at all, and the AI provider with one that does the same, so
`test_the_whole_outcome_loop_runs_with_no_provider_and_no_ai` is a real claim about cost and
availability rather than a hopeful one.

*That a manual listing is private.* job_listings was until now entirely public content, and
its read path said so in as many words. A manual listing is the student's own writing -- the
company a senior named, a description pasted out of an email -- so every path that can reach a
listing by id is checked against another student's account.
"""

import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.main import app
from app.services.job_hunt import service
from app.services.job_hunt.providers.base import JobProvider
from app.services.job_hunt.providers.factory import get_job_provider


class ForbiddenJobProvider(JobProvider):
    """Fails loudly rather than quietly returning nothing.

    A provider that returned an empty list would let a manual-entry bug pass as long as the
    test only asserted on rows. Raising means any accidental provider call shows up as the
    failure it is -- which is the whole point of a feature that must work when the provider is
    unavailable, unconfigured or out of quota.
    """

    name = "forbidden"
    attribution_text = ""
    attribution_url = None

    def search(self, query):
        raise AssertionError("manual job entry must never reach a job provider")


class ForbiddenAIProvider(AIProvider):
    """Manual entry is ingestion, not interpretation. A pasted job description is stored as
    typed; nothing here is summarised, parsed or scored by a model."""

    def generate_structured(self, prompt, schema):
        raise AssertionError("manual job entry must never reach an AI provider")

    def generate_text(self, prompt):
        raise AssertionError("manual job entry must never reach an AI provider")


@pytest.fixture()
def no_provider():
    """Both external dependencies replaced with ones that fail on use, for the whole test."""
    app.dependency_overrides[get_job_provider] = ForbiddenJobProvider
    app.dependency_overrides[get_ai_provider] = ForbiddenAIProvider
    yield
    app.dependency_overrides.pop(get_job_provider, None)
    app.dependency_overrides.pop(get_ai_provider, None)


def register(client, email: str) -> dict:
    client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def student(client):
    return register(client, "manual@example.com")


@pytest.fixture()
def other_student(client):
    return register(client, "manual-other@example.com")


def add_job(client, headers, **overrides):
    body = {"title": "Data Engineer", "company": "Contoso"}
    body.update(overrides)
    return client.post("/job-hunt/listings", json=body, headers=headers)


class TestCreatingAJobByHand:
    def test_a_job_found_off_platform_becomes_a_tracked_application(
        self, client, student, no_provider
    ):
        response = add_job(
            client,
            student,
            location="Hyderabad",
            url="https://careers.example.com/de-2026",
            description="Required: SQL, Python, Airflow.",
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "saved"
        assert body["source"] == "manual", "provenance is recorded by the server, not claimed"
        listing = body["listing"]
        assert listing["title"] == "Data Engineer"
        assert listing["company"] == "Contoso"
        assert listing["location"] == "Hyderabad"
        assert listing["url"] == "https://careers.example.com/de-2026"
        assert listing["description"] == "Required: SQL, Python, Airflow."
        assert listing["provider"] == "manual"

    def test_a_campus_placement_with_no_link_is_accepted(self, client, student, no_provider):
        """The case the migration exists for. A referral or a campus drive has no web page,
        and the product would be useless to those students if a URL were required."""
        response = add_job(client, student, title="Campus placement -- SDE", company=None)

        assert response.status_code == 201, response.text
        listing = response.json()["listing"]
        assert listing["url"] is None, "no link is null, never an empty string or a guess"
        assert listing["company"] is None

    def test_it_appears_in_the_tracker_immediately(self, client, student, no_provider):
        add_job(client, student)

        rows = client.get("/job-hunt/saved", headers=student).json()
        assert [row["listing"]["title"] for row in rows] == ["Data Engineer"]
        assert rows[0]["source"] == "manual"

    def test_no_match_is_invented_for_a_job_nothing_has_scored(
        self, client, student, no_provider
    ):
        """A tracker row shows a match percentage only when one was actually computed. A
        freshly entered job has not been scored against anything, and a 0 there would read as
        a verdict rather than as an absence."""
        body = add_job(client, student).json()

        assert body["match"] is None

    def test_a_manual_listing_is_never_stale(self, client, student, no_provider):
        """expires_at exists to stop a cached provider copy being shown as current after that
        provider's retention window. Nothing here was cached from anyone."""
        body = add_job(client, student).json()

        assert body["listing"]["is_stale"] is False

    def test_a_student_who_already_interviewed_can_say_so(self, client, student, no_provider):
        """Someone finding this product mid-process must not have to walk a real application
        backwards through a pipeline it has already passed."""
        response = add_job(client, student, status="interview")

        assert response.status_code == 201
        assert response.json()["status"] == "interview"

    def test_applied_at_is_stamped_when_a_job_is_entered_as_applied(
        self, client, student, no_provider
    ):
        body = add_job(client, student, status="applied").json()

        assert body["applied_at"] is not None


class TestWhatTheFormWillNotAccept:
    def test_a_job_with_no_title_is_refused(self, client, student, no_provider):
        assert client.post("/job-hunt/listings", json={}, headers=student).status_code == 422

    def test_a_whitespace_only_title_is_refused_rather_than_stored(
        self, client, student, no_provider
    ):
        """A field the student tabbed through holds " ", not None. Storing it would produce a
        tracker row with no name at all."""
        assert add_job(client, student, title="   ").status_code == 422

    def test_a_whitespace_only_company_is_stored_as_absent(self, client, student, no_provider):
        body = add_job(client, student, company="  ").json()

        assert body["listing"]["company"] is None, "not a company named one space"

    def test_a_title_longer_than_the_column_is_refused_not_truncated(
        self, client, student, no_provider
    ):
        """PostgreSQL raises on an over-long value and SQLite silently truncates, so the only
        behaviour that is the same in development and production is refusing at the edge."""
        assert add_job(client, student, title="x" * 501).status_code == 422

    def test_an_enormous_description_is_refused(self, client, student, no_provider):
        assert add_job(client, student, description="x" * 20_001).status_code == 422

    @pytest.mark.parametrize(
        "bad_url",
        [
            "javascript:alert(1)",
            "intent://evil",
            "http://localhost/jobs/1",
            "http://127.0.0.1/jobs/1",
            "http://169.254.169.254/latest/meta-data/",
            "not a url at all",
        ],
    )
    def test_a_link_this_app_will_not_hand_to_a_browser_is_refused_with_a_reason(
        self, client, student, no_provider, bad_url
    ):
        """A provider advert with a bad URL is dropped silently, because one bad advert must
        not fail a page of twenty. Here the student typed it, so silence would look like the
        link was accepted."""
        response = add_job(client, student, url=bad_url)

        assert response.status_code == 422
        assert "link" in response.json()["detail"].lower()

    def test_an_unknown_stage_is_refused(self, client, student, no_provider):
        assert add_job(client, student, status="hired").status_code == 422

    def test_nothing_is_written_when_the_stage_is_refused(self, client, student, no_provider):
        """The stage is checked before the listing is inserted. Checking it afterwards would
        leave a row of the student's text with nothing tracking it and no way back to it."""
        add_job(client, student, status="hired")

        assert client.get("/job-hunt/saved", headers=student).json() == []


class TestOwnership:
    def test_the_student_who_entered_it_can_open_it(self, client, student, no_provider):
        listing_id = add_job(client, student).json()["listing"]["id"]

        response = client.get(f"/job-hunt/jobs/{listing_id}", headers=student)

        assert response.status_code == 200
        assert response.json()["listing"]["title"] == "Data Engineer"

    def test_another_student_cannot_read_it(
        self, client, student, other_student, no_provider
    ):
        """job_listings was public content until this phase, and its read path said so. The
        moment a student types into one, that stops being true."""
        listing_id = add_job(
            client, student, description="Confidential referral from my senior."
        ).json()["listing"]["id"]

        response = client.get(f"/job-hunt/jobs/{listing_id}", headers=other_student)

        assert response.status_code == 404, "404, not 403 -- an id must not be confirmable"
        assert "Confidential" not in response.text

    def test_another_student_cannot_save_it(self, client, student, other_student, no_provider):
        """The listing id in this request body is the one Job Hunt id that does not arrive in
        a path scoped by its handler. Unguarded, saving someone else's manual job would put
        its title, company and description straight into the thief's own tracker."""
        listing_id = add_job(client, student).json()["listing"]["id"]

        response = client.post(
            "/job-hunt/saved", json={"listing_id": listing_id}, headers=other_student
        )

        assert response.status_code == 404
        assert client.get("/job-hunt/saved", headers=other_student).json() == []

    def test_another_student_cannot_read_its_skill_analysis(
        self, client, student, other_student, no_provider
    ):
        listing_id = add_job(client, student).json()["listing"]["id"]

        response = client.get(
            f"/job-hunt/jobs/{listing_id}/skill-analysis", headers=other_student
        )

        assert response.status_code == 404

    def test_two_students_entering_the_same_job_get_their_own_copies(
        self, client, student, other_student, no_provider
    ):
        """Deduplication is scoped to one student on purpose. Sharing a row between two
        students who happened to paste the same link would hand the second one the first's
        typing, and let each edit what the other sees."""
        url = "https://careers.example.com/shared-posting"
        mine = add_job(client, student, url=url, company="As I typed it").json()
        theirs = add_job(client, other_student, url=url, company="As they typed it").json()

        assert mine["listing"]["id"] != theirs["listing"]["id"]
        assert mine["listing"]["company"] == "As I typed it"
        assert theirs["listing"]["company"] == "As they typed it"


class TestReAddingTheSameJob:
    def test_the_same_link_twice_produces_one_tracked_job(self, client, student, no_provider):
        """Two jobs are the same job when they are at the same link -- two different openings
        at one company have two different pages, so this is an exact claim, not a guess."""
        url = "https://careers.example.com/de-2026"
        first = add_job(client, student, url=url).json()
        second = add_job(client, student, url=url).json()

        assert first["id"] == second["id"]
        assert len(client.get("/job-hunt/saved", headers=student).json()) == 1

    def test_re_adding_a_job_does_not_rewind_its_stage(self, client, student, no_provider):
        """The destructive case. A student who re-pastes a link they had already moved to
        "interview" must not have it reset to whatever the form defaulted to."""
        url = "https://careers.example.com/de-2026"
        saved_id = add_job(client, student, url=url, status="interview").json()["id"]

        again = add_job(client, student, url=url, status="saved").json()

        assert again["id"] == saved_id
        assert again["status"] == "interview", "a real application was not walked backwards"

    def test_two_link_less_jobs_are_two_jobs(self, client, student, no_provider):
        """Deliberate, and the documented limit of the deduplication rule. With no link there
        is no key: a student may genuinely be tracking two different campus roles at the same
        company with the same title, and guessing the second is a duplicate would silently
        discard a real application."""
        add_job(client, student, title="SDE", company="Contoso")
        add_job(client, student, title="SDE", company="Contoso")

        assert len(client.get("/job-hunt/saved", headers=student).json()) == 2


class TestTheOutcomeLoop:
    def test_the_whole_outcome_loop_runs_with_no_provider_and_no_ai(
        self, client, student, no_provider
    ):
        """The claim this phase exists to make. Both external dependencies raise if touched,
        so reaching the end of this test is proof the loop is provider-independent rather than
        merely believed to be."""
        saved_id = add_job(
            client, student, url="https://careers.example.com/de-2026"
        ).json()["id"]

        goal = client.post(
            "/career-goals",
            json={
                "title": "Data Engineer",
                "company": "Contoso",
                "job_description": "Required: SQL, Python, Airflow.",
            },
            headers=student,
        )
        assert goal.status_code == 201, goal.text
        goal_id = goal.json()["id"]

        link = client.put(
            f"/career-goals/{goal_id}/application",
            json={"saved_job_id": saved_id},
            headers=student,
        )
        assert link.status_code == 200, link.text
        assert link.json()["application_status"] == "saved"

        for stage in ("applied", "assessment", "interview", "offer"):
            moved = client.patch(
                f"/job-hunt/saved/{saved_id}", json={"status": stage}, headers=student
            )
            assert moved.status_code == 200, moved.text
            assert moved.json()["status"] == stage
            # What Home reads: the goal's own view of its application.
            goals = client.get("/career-goals", headers=student).json()
            assert goals[0]["application_status"] == stage

        detail = client.get(f"/career-goals/{goal_id}", headers=student).json()
        assert detail["application"]["outcome"] == "offer"
        assert detail["application"]["is_terminal"] is True

    @pytest.mark.parametrize("outcome", ["rejected", "closed"])
    def test_the_other_two_terminal_outcomes_reach_the_goal_as_well(
        self, client, student, no_provider, outcome
    ):
        saved_id = add_job(client, student).json()["id"]
        goal_id = client.post(
            "/career-goals",
            json={"title": "Data Engineer", "company": "Contoso", "job_description": "SQL."},
            headers=student,
        ).json()["id"]
        client.put(
            f"/career-goals/{goal_id}/application",
            json={"saved_job_id": saved_id},
            headers=student,
        )

        client.patch(f"/job-hunt/saved/{saved_id}", json={"status": outcome}, headers=student)

        detail = client.get(f"/career-goals/{goal_id}", headers=student).json()
        assert detail["application"]["outcome"] == outcome
        assert detail["application"]["is_terminal"] is True

    def test_a_job_entered_at_interview_links_without_being_reset(
        self, client, student, no_provider
    ):
        """Section 42's case end to end: found the product mid-process, entered the real
        stage, linked it to the goal, and the goal shows the real stage."""
        saved_id = add_job(client, student, status="interview").json()["id"]
        goal_id = client.post(
            "/career-goals",
            json={"title": "Data Engineer", "company": "Contoso", "job_description": "SQL."},
            headers=student,
        ).json()["id"]

        link = client.put(
            f"/career-goals/{goal_id}/application",
            json={"saved_job_id": saved_id},
            headers=student,
        )

        assert link.json()["application_status"] == "interview"

    def test_another_student_cannot_link_a_manual_job_they_do_not_own(
        self, client, student, other_student, no_provider
    ):
        saved_id = add_job(client, student).json()["id"]
        their_goal = client.post(
            "/career-goals",
            json={"title": "Data Engineer", "company": "Contoso", "job_description": "SQL."},
            headers=other_student,
        ).json()["id"]

        response = client.put(
            f"/career-goals/{their_goal}/application",
            json={"saved_job_id": saved_id},
            headers=other_student,
        )

        assert response.status_code == 404

    def test_a_manual_job_already_claimed_by_one_goal_is_refused_by_another(
        self, client, student, no_provider
    ):
        """The same arbiter provider-sourced jobs get: one application belongs to at most one
        goal, enforced by a unique constraint rather than by a check the second request could
        race past."""
        saved_id = add_job(client, student).json()["id"]
        goals = [
            client.post(
                "/career-goals",
                json={"title": title, "company": "Contoso", "job_description": "SQL."},
                headers=student,
            ).json()["id"]
            for title in ("Data Engineer", "Analytics Engineer")
        ]
        client.put(
            f"/career-goals/{goals[0]}/application",
            json={"saved_job_id": saved_id},
            headers=student,
        )

        second = client.put(
            f"/career-goals/{goals[1]}/application",
            json={"saved_job_id": saved_id},
            headers=student,
        )

        assert second.status_code == 409

    def test_linking_the_same_manual_job_to_the_same_goal_twice_is_safe(
        self, client, student, no_provider
    ):
        saved_id = add_job(client, student).json()["id"]
        goal_id = client.post(
            "/career-goals",
            json={"title": "Data Engineer", "company": "Contoso", "job_description": "SQL."},
            headers=student,
        ).json()["id"]

        first = client.put(
            f"/career-goals/{goal_id}/application",
            json={"saved_job_id": saved_id},
            headers=student,
        )
        repeat = client.put(
            f"/career-goals/{goal_id}/application",
            json={"saved_job_id": saved_id},
            headers=student,
        )

        assert (first.status_code, repeat.status_code) == (200, 200)

    def test_removing_it_from_the_tracker_does_not_make_it_public(
        self, client, student, other_student, no_provider
    ):
        """The listing row outlives the tracker row on purpose -- re-adding the same link then
        restores the same job rather than creating a second one. Ownership has to outlive it
        too, or unsaving a job would quietly publish what the student wrote."""
        added = add_job(client, student, url="https://careers.example.com/de").json()
        client.delete(f"/job-hunt/saved/{added['id']}", headers=student)

        listing_id = added["listing"]["id"]
        assert client.get(f"/job-hunt/jobs/{listing_id}", headers=other_student).status_code == 404
        assert client.get(f"/job-hunt/jobs/{listing_id}", headers=student).status_code == 200

    def test_a_manual_job_can_be_removed_from_the_tracker(self, client, student, no_provider):
        saved_id = add_job(client, student).json()["id"]

        assert client.delete(f"/job-hunt/saved/{saved_id}", headers=student).status_code == 204
        assert client.get("/job-hunt/saved", headers=student).json() == []


def test_no_job_provider_can_claim_the_manual_namespace():
    """`provider == "manual"` is not just a label -- it is what tells a typed-in listing apart
    from a public advert everywhere downstream. A provider adapter answering to the same name
    would blur the two, and the read guard would start hiding public listings from the students
    who searched for them.
    """
    from app.services.job_hunt.providers.adzuna import AdzunaJobProvider
    from app.services.job_hunt.providers.careerjet import CareerjetJobProvider
    from app.services.job_hunt.providers.jobvetta import JobvettaJobProvider
    from app.services.job_hunt.providers.null import NullJobProvider

    names = {
        AdzunaJobProvider.name,
        CareerjetJobProvider.name,
        JobvettaJobProvider.name,
        NullJobProvider.name,
    }

    assert service.MANUAL_PROVIDER not in names


class TestProviderIngestionIsUnchanged:
    """Phase 4 adds a second door into job_listings. These prove it did not alter the first."""

    def test_a_provider_listing_is_still_readable_by_any_student(
        self, client, student, other_student
    ):
        """The asymmetry that makes a shared listing table correct: a public advert has
        nothing in it belonging to any one student, and many legitimately match against it."""
        from tests.api.test_job_hunt import FakeJobProvider, provider_job

        app.dependency_overrides[get_job_provider] = lambda: FakeJobProvider(
            jobs=[provider_job()]
        )
        try:
            client.put(
                "/job-hunt/profile",
                json={"target_role_title": "Software Engineer", "declared_skills": ["python"]},
                headers=student,
            )
            results = client.post(
                "/job-hunt/search", json={"keywords": "engineer"}, headers=student
            )
            assert results.status_code == 200, results.text
            listing_id = results.json()["results"][0]["listing"]["id"]

            seen_by_someone_else = client.get(
                f"/job-hunt/jobs/{listing_id}", headers=other_student
            )
        finally:
            app.dependency_overrides.pop(get_job_provider, None)

        assert seen_by_someone_else.status_code == 200
        assert seen_by_someone_else.json()["listing"]["provider"] == "fake"

    def test_a_provider_listing_can_still_be_saved_by_anyone(
        self, client, student, other_student
    ):
        from tests.api.test_job_hunt import FakeJobProvider, provider_job

        app.dependency_overrides[get_job_provider] = lambda: FakeJobProvider(
            jobs=[provider_job()]
        )
        try:
            client.put(
                "/job-hunt/profile",
                json={"target_role_title": "Software Engineer", "declared_skills": ["python"]},
                headers=student,
            )
            listing_id = client.post(
                "/job-hunt/search", json={"keywords": "engineer"}, headers=student
            ).json()["results"][0]["listing"]["id"]

            saved = client.post(
                "/job-hunt/saved",
                json={"listing_id": listing_id, "source": "recommended"},
                headers=other_student,
            )
        finally:
            app.dependency_overrides.pop(get_job_provider, None)

        assert saved.status_code == 201
        assert saved.json()["source"] == "recommended"
