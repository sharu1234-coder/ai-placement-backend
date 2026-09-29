"""What the intelligence layer is, and is not, allowed to do with a real-world outcome.

Phase 2E's whole risk sits here. Feeding application outcomes into Career Twin and Next Best
Action is the point of the phase; letting them become a statement about the candidate's skills
would be a product lie that no amount of test coverage elsewhere makes up for.

So these tests are mostly about restraint:

  - a rejection must not move evidence_coverage_percent, because coverage is a claim about evidence
    for role skills and a rejection is a claim about one employer's decision;
  - a rejection must not create, worsen or invent a weakness;
  - a live interview stage may only influence the FALLBACK recommendation, never outrank a
    measured weakness;
  - nothing here may reach an AI provider.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- registers every model with Base.metadata
from app.database.base import Base
from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.saved_job import SavedJob
from app.models.user import User
from app.services import application_outcome, next_best_action_service


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'outcome.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def profile_id(db):
    user = User(email="outcome@example.com", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile.id


def _application(db, profile_id, status: str, key: str) -> SavedJob:
    listing = JobListing(
        provider="test",
        provider_job_id=key,
        url="https://example.invalid/job",
        title="Data Engineer",
        company="Microsoft",
        fetched_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    saved = SavedJob(profile_id=profile_id, listing_id=listing.id, status=status)
    db.add(saved)
    db.commit()
    db.refresh(saved)
    return saved


# ==========================================================================================
# The counts Career Twin reports
# ==========================================================================================


def test_a_profile_with_no_applications_reports_zeros_not_a_percentage(db, profile_id):
    context = application_outcome.context_for_profile(db, profile_id)

    assert context.total == 0
    assert context.offers == context.rejections == 0
    assert context.has_any_application is False


def test_the_counts_describe_what_actually_happened(db, profile_id):
    _application(db, profile_id, "applied", "a")
    _application(db, profile_id, "interview", "b")
    _application(db, profile_id, "rejected", "c")
    _application(db, profile_id, "offer", "d")
    _application(db, profile_id, "saved", "e")

    context = application_outcome.context_for_profile(db, profile_id)

    assert context.total == 5
    assert context.live == 2  # applied + interview; "saved" is not an application
    assert context.at_interview_stage == 1
    assert context.offers == 1
    assert context.rejections == 1


def test_one_students_applications_never_appear_in_anothers_context(db, profile_id):
    """Profile scoping on the aggregate itself, not merely on the endpoint above it."""
    other_user = User(email="other-outcome@example.com", password_hash="x")
    db.add(other_user)
    db.commit()
    db.refresh(other_user)
    other_profile = StudentProfile(user_id=other_user.id)
    db.add(other_profile)
    db.commit()
    db.refresh(other_profile)

    _application(db, profile_id, "offer", "mine")
    _application(db, other_profile.id, "rejected", "theirs")

    mine = application_outcome.context_for_profile(db, profile_id)
    theirs = application_outcome.context_for_profile(db, other_profile.id)

    assert (mine.offers, mine.rejections) == (1, 0)
    assert (theirs.offers, theirs.rejections) == (0, 1)


def test_counting_applications_runs_one_query_regardless_of_how_many_there_are(db, profile_id):
    """The N+1 guard. This runs inside career twin and next-best-action reads, which are
    already the two heaviest composed responses in the product."""
    for n in range(12):
        _application(db, profile_id, "applied", f"job-{n}")

    statements: list = []
    from sqlalchemy import event

    engine = db.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        if "saved_jobs" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        context = application_outcome.context_for_profile(db, profile_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert context.total == 12
    assert len(statements) == 1, f"expected one query, got {len(statements)}"


# ==========================================================================================
# Next Best Action: restraint under outcome
# ==========================================================================================


class _RecordingProvider:
    """Any call at all is a failure -- the outcome path must never reach a provider."""

    def __init__(self):
        self.calls = 0

    def generate_structured(self, prompt, schema):
        self.calls += 1
        raise AssertionError("the outcome path must not call AI")

    def generate_text(self, prompt: str) -> str:
        self.calls += 1
        raise AssertionError("the outcome path must not call AI")


def test_a_live_interview_produces_a_grounded_non_causal_recommendation(db, profile_id):
    """The one place an application changes what is recommended. It may state the fact; it may
    not explain the student to themselves."""
    _application(db, profile_id, "interview", "live")

    action = next_best_action_service._live_interview_action(
        application_outcome.context_for_profile(db, profile_id)
    )

    assert action is not None
    reasons = " ".join(action.reasons).lower()
    assert "interview stage" in reasons
    for forbidden in ("because", "you lack", "weak", "failed", "unqualified", "rejected"):
        assert forbidden not in reasons, f"recommendation made an unsupported claim: {forbidden!r}"


def test_the_reason_line_counts_correctly_and_reads_correctly(db, profile_id):
    """"1 applications" reads as a bug, and a student who spots one undercounts everything
    else on the screen."""
    _application(db, profile_id, "interview", "one")
    single = next_best_action_service._live_interview_action(
        application_outcome.context_for_profile(db, profile_id)
    )
    assert "1 application at the interview stage" in " ".join(single.reasons)

    _application(db, profile_id, "interview", "two")
    plural = next_best_action_service._live_interview_action(
        application_outcome.context_for_profile(db, profile_id)
    )
    assert "2 applications at the interview stage" in " ".join(plural.reasons)


def test_no_live_interview_means_no_outcome_driven_action(db, profile_id):
    """Applications that are merely saved, applied or finished must not trigger it. Only a
    live interview stage is a reason to practise interviewing right now."""
    for status in ("saved", "applied", "offer", "rejected", "closed", "dismissed"):
        db.query(SavedJob).delete()
        db.commit()
        _application(db, profile_id, status, f"k-{status}")
        action = next_best_action_service._live_interview_action(
            application_outcome.context_for_profile(db, profile_id)
        )
        assert action is None, f"{status} must not drive an interview recommendation"


def test_an_offer_does_not_keep_pushing_interview_practice(db, profile_id):
    """Section 30's rule: once an offer exists that application is resolved, and the system
    must stop behaving as though it is still live."""
    _application(db, profile_id, "offer", "won")

    assert next_best_action_service._live_interview_action(
        application_outcome.context_for_profile(db, profile_id)
    ) is None


def test_the_outcome_action_reuses_existing_vocabulary(db, profile_id):
    """No new tier, no new destination, no new confidence band -- so the Android destination
    table already resolves it and nothing has to decide what a new band means."""
    _application(db, profile_id, "interview", "live")

    action = next_best_action_service._live_interview_action(
        application_outcome.context_for_profile(db, profile_id)
    )

    assert action.tier in next_best_action_service._DESTINATION_BY_TIER
    assert action.destination == next_best_action_service._DESTINATION_BY_TIER[action.tier]
    assert action.confidence == next_best_action_service._CONFIDENCE_BY_TIER[action.tier]


def test_an_empty_context_is_handled_rather_than_assumed_away(db, profile_id):
    assert next_best_action_service._live_interview_action(None) is None
    assert next_best_action_service._live_interview_action(
        application_outcome.context_for_statuses([])
    ) is None


# ==========================================================================================
# Career Twin reports the same counts on every path it can return through
# ==========================================================================================


def test_every_career_twin_return_path_reports_the_application_counts(db, profile_id):
    """A student with no target role still has applications, and must not be told they have
    none.

    This is a real regression. The counts were first added only to the branch that computes a
    readiness number, so the two early returns -- no role selected, and a role with no defined
    skills -- fell back to the schema defaults and reported 0 offers to a student holding one.
    A count that says zero when the truth is one is precisely the quiet falsehood this feature
    exists to avoid, and it is invisible unless the paths are compared.
    """
    import inspect

    from app.services import career_twin_service

    source = inspect.getsource(career_twin_service.get_current)
    constructions = source.count("CareerTwinRead(")
    reported = source.count("_application_fields(applications)")

    assert constructions > 1, "expected several return paths; the guard below assumes it"
    assert reported == constructions, (
        f"get_current builds CareerTwinRead {constructions} times but reports application "
        f"counts on only {reported} of them -- the remaining path(s) would report 0 "
        f"applications for a student who has some"
    )


def test_the_counts_helper_covers_every_count_the_schema_declares(db, profile_id):
    """Guards the other half: a field added to the schema but not to the helper would default
    to 0 on every path at once, which is harder to spot than missing it on one."""
    from app.schemas.career_twin import CareerTwinRead
    from app.services.career_twin_service import _application_fields

    produced = set(_application_fields(application_outcome.context_for_statuses(["offer"])))
    declared = {
        name for name in CareerTwinRead.model_fields
        if name.startswith("application_") or name.startswith("applications_")
    }

    assert produced == declared, f"schema and helper disagree: {declared ^ produced}"
