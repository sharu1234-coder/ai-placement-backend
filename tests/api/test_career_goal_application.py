"""Phase 2E -- the link between a career goal and the real-world application it is pursued
through, end to end through the API.

What makes this worth testing is not the column. It is everything the column is allowed and not
allowed to do: that one student can never reach another's application through their own goal,
that two goals cannot both claim one application however the requests interleave, that linking
costs no AI call, and -- most importantly -- that an outcome never becomes a claim about the
candidate's skills.

Conventions are test_career_goals.py's, because this IS that feature gaining a field: same
signup helper, same `_db()` session, same fake provider, so the two modules cannot drift into
two different ideas of what a career goal is.
"""

import datetime

import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.jd import AIParsedJD, JDRequirementItem
from app.ai.schemas.resume import ParsedResume
from app.api.deps import get_db
from app.main import app
from app.models.career_goal import CareerGoal
from app.models.job_listing import JobListing
from app.models.saved_job import SAVED_JOB_STATUSES, SavedJob

PARSED_JD = AIParsedJD(
    role_title="Data Engineer",
    required_skills=[JDRequirementItem(value="SQL", is_explicit=True)],
    preferred_skills=[],
    responsibilities=["Build pipelines"],
    tools_technologies=[],
    experience_expectation="2+ years",
    education_eligibility="Bachelor's degree",
    other_signals=[],
    parsing_confidence=0.9,
)


class CountingAIProvider(AIProvider):
    """Records every call so a test can prove a code path spent nothing.

    "Linking runs no AI" is a cost guarantee, and the only way to hold it is to count.
    """

    def __init__(self):
        self.calls = 0

    def generate_structured(self, prompt, schema):
        self.calls += 1
        if schema is AIParsedJD:
            return PARSED_JD
        if schema is ParsedResume:
            return ParsedResume(skills=[], education=[], experience=[], projects=[], certifications=[])
        raise AssertionError(f"unexpected schema {schema}")

    def generate_text(self, prompt: str) -> str:  # pragma: no cover - not reached here
        self.calls += 1
        return "unused"


@pytest.fixture()
def provider():
    p = CountingAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: p
    yield p
    app.dependency_overrides.pop(get_ai_provider, None)


def _headers(client, email):
    client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": email, "password": "supersecret123"}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_headers(client):
    return _headers(client, "goal-app@example.com")


@pytest.fixture()
def other_auth_headers(client):
    return _headers(client, "goal-app-other@example.com")


def _db():
    return next(app.dependency_overrides[get_db]())


def _create_goal(client, headers, **overrides):
    payload = {
        "title": "Data Engineer",
        "company": "Microsoft",
        "location": "Dubai",
        "job_description": "We are hiring a Data Engineer. Required: SQL.",
    }
    payload.update(overrides)
    response = client.post("/career-goals", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _profile_id(email: str) -> int:
    """This student's profile id, creating the profile if signup alone has not yet.

    Profiles are created lazily by get_or_create_profile on first use, so a user who has only
    signed up has no row yet -- which is exactly the state the cross-user tests below put the
    victim in. Going through the app's own function rather than a raw INSERT keeps the fixture
    honest about how profiles actually come into existence.
    """
    from app.models.user import User
    from app.services.profile_service import get_or_create_profile

    db = _db()
    user = db.query(User).filter(User.email == email).one()
    return get_or_create_profile(db, user).id


def _saved_job(profile_email: str, *, status: str = "saved", title: str = "Data Engineer") -> int:
    """A tracked application, created directly -- the job-hunt save path needs a provider search
    behind it, and what these tests need is the row it would produce."""
    db = _db()
    listing = JobListing(
        provider="test",
        provider_job_id=f"job-{title}-{status}-{datetime.datetime.now().timestamp()}",
        url="https://example.invalid/job",
        title=title,
        company="Microsoft",
        fetched_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    saved = SavedJob(
        profile_id=_profile_id(profile_email),
        listing_id=listing.id,
        status=status,
        applied_at=(
            datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            if status != "saved"
            else None
        ),
    )
    db.add(saved)
    db.commit()
    db.refresh(saved)
    return saved.id


def _link(client, headers, goal_id, saved_job_id):
    return client.put(
        f"/career-goals/{goal_id}/application",
        json={"saved_job_id": saved_job_id},
        headers=headers,
    )


# ==========================================================================================
# A -- the relationship itself
# ==========================================================================================


def test_a_goal_starts_unlinked_and_says_so_without_pretending_nothing_was_applied_to(
    client, auth_headers
):
    """NULL means "not linked", which is not the same as "has not applied". A goal with no
    link says nothing at all about the student's applications elsewhere."""
    goal_id = _create_goal(client, auth_headers)

    detail = client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()

    assert detail["application"] is None
    listed = client.get("/career-goals", headers=auth_headers).json()
    assert listed[0]["application_status"] is None


def test_linking_surfaces_the_application_on_the_goal(client, auth_headers):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="interview")

    assert _link(client, auth_headers, goal_id, saved_id).status_code == 200

    application = client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()["application"]
    assert application["saved_job_id"] == saved_id
    assert application["status"] == "interview"
    assert application["listing_title"] == "Data Engineer"
    # Still running, so there is no outcome. Not "pending", not "" -- None.
    assert application["outcome"] is None
    assert application["is_terminal"] is False
    assert application["has_applied"] is True


def test_the_list_row_carries_the_application_status(client, auth_headers):
    """The row must be able to say "Interview stage" beside a goal whose in-app preparation has
    not started -- those are different facts and the screen shows both."""
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="interview")
    _link(client, auth_headers, goal_id, saved_id)

    row = client.get("/career-goals", headers=auth_headers).json()[0]

    assert row["application_status"] == "interview"
    # Unchanged, and deliberately so. The application being at interview stage says nothing
    # about whether this goal's job description has been analyzed, and the row must not let one
    # imply the other -- an unanalyzed goal is "not_analyzed" whatever the tracker says.
    assert row["preparation_state"] == "not_analyzed"


def test_unlinking_leaves_both_rows_alive(client, auth_headers):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="applied")
    _link(client, auth_headers, goal_id, saved_id)

    assert client.delete(f"/career-goals/{goal_id}/application", headers=auth_headers).status_code == 200

    assert client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()["application"] is None
    db = _db()
    assert db.get(SavedJob, saved_id) is not None
    assert db.get(CareerGoal, goal_id) is not None


# ==========================================================================================
# B -- idempotency and conflict
# ==========================================================================================


def test_linking_the_same_pair_twice_is_not_an_error(client, auth_headers):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com")

    assert _link(client, auth_headers, goal_id, saved_id).status_code == 200
    assert _link(client, auth_headers, goal_id, saved_id).status_code == 200

    db = _db()
    assert db.get(CareerGoal, goal_id).saved_job_id == saved_id


def test_unlinking_an_unlinked_goal_is_not_an_error(client, auth_headers):
    goal_id = _create_goal(client, auth_headers)
    assert client.delete(f"/career-goals/{goal_id}/application", headers=auth_headers).status_code == 200


def test_two_goals_cannot_claim_the_same_application(client, auth_headers):
    """The product rule, enforced by the unique constraint rather than by a check the second
    writer could race past."""
    first = _create_goal(client, auth_headers)
    second = _create_goal(client, auth_headers, title="Data Engineer II")
    saved_id = _saved_job("goal-app@example.com")

    assert _link(client, auth_headers, first, saved_id).status_code == 200
    conflict = _link(client, auth_headers, second, saved_id)

    assert conflict.status_code == 409
    db = _db()
    assert db.get(CareerGoal, first).saved_job_id == saved_id
    assert db.get(CareerGoal, second).saved_job_id is None


def test_relinking_a_goal_to_a_different_application_replaces_the_link(client, auth_headers):
    goal_id = _create_goal(client, auth_headers)
    first = _saved_job("goal-app@example.com", title="First")
    second = _saved_job("goal-app@example.com", title="Second")
    _link(client, auth_headers, goal_id, first)

    assert _link(client, auth_headers, goal_id, second).status_code == 200

    db = _db()
    assert db.get(CareerGoal, goal_id).saved_job_id == second
    # The application it was pointed at before is untouched -- relinking a goal is not a reason
    # to lose a tracked application.
    assert db.get(SavedJob, first) is not None


# ==========================================================================================
# C -- ownership. The reason this endpoint is dangerous if it is wrong.
# ==========================================================================================


def test_one_student_cannot_link_their_goal_to_another_students_application(
    client, auth_headers, other_auth_headers
):
    """The IDOR that would look like a feature: link my goal to your application and read your
    status out of my own goal."""
    my_goal = _create_goal(client, auth_headers)
    their_job = _saved_job("goal-app-other@example.com", status="offer")

    response = _link(client, auth_headers, my_goal, their_job)

    assert response.status_code == 404
    db = _db()
    assert db.get(CareerGoal, my_goal).saved_job_id is None


def test_one_student_cannot_link_another_students_goal(client, auth_headers, other_auth_headers):
    their_goal = _create_goal(client, other_auth_headers)
    my_job = _saved_job("goal-app@example.com")

    assert _link(client, auth_headers, their_goal, my_job).status_code == 404


def test_one_student_cannot_unlink_another_students_goal(client, auth_headers, other_auth_headers):
    their_goal = _create_goal(client, other_auth_headers)
    their_job = _saved_job("goal-app-other@example.com")
    _link(client, other_auth_headers, their_goal, their_job)

    assert client.delete(f"/career-goals/{their_goal}/application", headers=auth_headers).status_code == 404

    db = _db()
    assert db.get(CareerGoal, their_goal).saved_job_id == their_job


def test_a_missing_and_a_foreign_saved_job_are_answered_identically(
    client, auth_headers, other_auth_headers
):
    """No id oracle: the response for somebody else's application must be byte-identical to the
    response for one that never existed."""
    goal_id = _create_goal(client, auth_headers)
    their_job = _saved_job("goal-app-other@example.com")

    foreign = _link(client, auth_headers, goal_id, their_job)
    missing = _link(client, auth_headers, goal_id, 9_999_999)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["detail"].replace(str(their_job), "X") == missing.json()[
        "detail"
    ].replace("9999999", "X")


def test_linking_requires_authentication(client, auth_headers):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com")

    assert client.put(
        f"/career-goals/{goal_id}/application", json={"saved_job_id": saved_id}
    ).status_code == 401


def test_the_request_body_cannot_smuggle_ownership(client, auth_headers, other_auth_headers):
    """profile_id in the body must change nothing -- ownership comes from the token alone."""
    goal_id = _create_goal(client, auth_headers)
    their_job = _saved_job("goal-app-other@example.com")

    response = client.put(
        f"/career-goals/{goal_id}/application",
        json={"saved_job_id": their_job, "profile_id": _profile_id("goal-app-other@example.com")},
        headers=auth_headers,
    )

    assert response.status_code == 404


# ==========================================================================================
# D -- outcome semantics, across every status the tracker actually supports
# ==========================================================================================


@pytest.mark.parametrize(
    "status,outcome,terminal,applied",
    [
        ("saved", None, False, False),
        ("applied", None, False, True),
        ("assessment", None, False, True),
        ("interview", None, False, True),
        ("offer", "offer", True, True),
        ("rejected", "rejected", True, True),
        ("closed", "closed", True, False),
        ("dismissed", "closed", True, False),
    ],
)
def test_every_tracker_status_maps_to_a_defined_outcome(
    client, auth_headers, status, outcome, terminal, applied
):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status=status)
    _link(client, auth_headers, goal_id, saved_id)

    application = client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()["application"]

    assert application["status"] == status
    assert application["outcome"] == outcome
    assert application["is_terminal"] is terminal
    assert application["has_applied"] is applied


def test_the_parametrization_above_covers_every_status_the_model_allows():
    """A guard, not a tautology: when someone adds a ninth status to the tracker, the mapping
    above stops being exhaustive and this fails rather than a new status silently reading as
    "no outcome"."""
    from app.services import application_outcome

    covered = {"saved", "applied", "assessment", "interview", "offer", "rejected", "closed", "dismissed"}
    assert set(SAVED_JOB_STATUSES) == covered
    for status in SAVED_JOB_STATUSES:
        # Every status must produce a definite answer from each predicate rather than raising.
        assert isinstance(application_outcome.is_terminal(status), bool)
        assert isinstance(application_outcome.has_applied(status), bool)


def test_a_rejection_never_becomes_a_statement_about_the_candidate(client, auth_headers):
    """The one thing this phase must not do. A rejection is one employer's decision, and the
    response must not turn it into a skill verdict anywhere on the screen."""
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="rejected")
    _link(client, auth_headers, goal_id, saved_id)

    body = client.get(f"/career-goals/{goal_id}", headers=auth_headers).text.lower()

    for forbidden in (
        "you lack",
        "unqualified",
        "not qualified",
        "unsuitable",
        "does not have",
        "you failed",
        "because you",
        "rejected because",
    ):
        assert forbidden not in body, f"outcome text made a causal/skill claim: {forbidden!r}"


def test_an_outcome_does_not_alter_the_evidence_verdicts(client, auth_headers, provider):
    """Skill evidence is computed by the evidence engine from what the student can prove. A
    rejection must leave every verdict exactly where it was."""
    goal_id = _create_goal(client, auth_headers)
    client.post(f"/career-goals/{goal_id}/analyze", headers=auth_headers)
    before = client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()

    saved_id = _saved_job("goal-app@example.com", status="rejected")
    _link(client, auth_headers, goal_id, saved_id)
    after = client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()

    for bucket in ("aligned", "needs_attention", "unknown"):
        assert before[bucket] == after[bucket]
    assert before["progress"]["evidence_coverage_percent"] == after["progress"]["evidence_coverage_percent"]


# ==========================================================================================
# E -- cost. Linking is a pointer write and must never reach a provider.
# ==========================================================================================


def test_linking_and_reading_an_application_never_calls_ai(client, auth_headers, provider):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="applied")

    _link(client, auth_headers, goal_id, saved_id)
    client.get(f"/career-goals/{goal_id}", headers=auth_headers)
    client.get("/career-goals", headers=auth_headers)
    client.delete(f"/career-goals/{goal_id}/application", headers=auth_headers)

    assert provider.calls == 0


def test_a_status_change_on_the_tracker_never_calls_ai(client, auth_headers, provider):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com")
    _link(client, auth_headers, goal_id, saved_id)

    client.patch(f"/job-hunt/saved/{saved_id}", json={"status": "offer"}, headers=auth_headers)
    application = client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()["application"]

    assert application["status"] == "offer"
    assert provider.calls == 0


# ==========================================================================================
# F -- the tracker stays the single source of truth
# ==========================================================================================


def test_the_goal_follows_the_tracker_through_the_whole_pipeline(client, auth_headers):
    """The loop the phase exists for: the student moves the application in the tracker and the
    goal reflects it, with no second copy of the state to fall out of step."""
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com")
    _link(client, auth_headers, goal_id, saved_id)

    seen = []
    for status in ("applied", "assessment", "interview", "rejected"):
        client.patch(f"/job-hunt/saved/{saved_id}", json={"status": status}, headers=auth_headers)
        application = client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()["application"]
        seen.append((application["status"], application["outcome"], application["is_terminal"]))

    assert seen == [
        ("applied", None, False),
        ("assessment", None, False),
        ("interview", None, False),
        ("rejected", "rejected", True),
    ]


def test_applied_at_appears_once_the_student_applies(client, auth_headers):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="saved")
    _link(client, auth_headers, goal_id, saved_id)

    assert client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()["application"][
        "applied_at"
    ] is None

    client.patch(f"/job-hunt/saved/{saved_id}", json={"status": "applied"}, headers=auth_headers)

    assert client.get(f"/career-goals/{goal_id}", headers=auth_headers).json()["application"][
        "applied_at"
    ] is not None


# ==========================================================================================
# G -- deletion. The FK must never take a row down with it.
# ==========================================================================================


def test_deleting_the_application_clears_the_link_and_keeps_the_goal(client, auth_headers):
    """ON DELETE SET NULL, proven through the real endpoint. The student tidied their tracker;
    they did not abandon the goal."""
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="applied")
    _link(client, auth_headers, goal_id, saved_id)

    assert client.delete(f"/job-hunt/saved/{saved_id}", headers=auth_headers).status_code == 204

    detail = client.get(f"/career-goals/{goal_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["application"] is None
    db = _db()
    assert db.get(CareerGoal, goal_id).saved_job_id is None


def test_deleting_a_linked_goal_keeps_the_application(client, auth_headers):
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="applied")
    _link(client, auth_headers, goal_id, saved_id)

    assert client.delete(f"/career-goals/{goal_id}", headers=auth_headers).status_code == 204

    db = _db()
    assert db.get(SavedJob, saved_id) is not None


def test_account_deletion_removes_both_sides_with_a_link_in_place(client, auth_headers):
    """The link must not break the deletion order: saved_jobs are deleted before career_goals,
    which is exactly the order that would raise against a plain foreign key."""
    goal_id = _create_goal(client, auth_headers)
    saved_id = _saved_job("goal-app@example.com", status="offer")
    _link(client, auth_headers, goal_id, saved_id)

    response = client.request(
        "DELETE", "/users/me", json={"password": "supersecret123"}, headers=auth_headers
    )
    assert response.status_code in (204, 200), response.text

    db = _db()
    assert db.get(CareerGoal, goal_id) is None
    assert db.get(SavedJob, saved_id) is None
