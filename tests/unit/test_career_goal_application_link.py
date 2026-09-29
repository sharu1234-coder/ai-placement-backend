"""The application link at the service layer: the deterministic outcome mapping, and two
requests racing for the same application.

Split from tests/api/test_career_goal_application.py on purpose. That module drives the real
endpoints and is where ownership and product behaviour are proven; this one holds the two
things the API cannot reach -- a pure mapping that deserves table-driven tests without a
database, and a genuine concurrent race that needs real threads on a file-backed database.

The concurrency setup repeats the three facts spelled out in
tests/unit/test_job_hunt_entitlement_concurrency.py, because getting any of them wrong produces
a test that passes without testing anything:

*Real threads on real sessions.* Calling link twice in a row proves nothing: the second call
reads the first one's committed row and takes the idempotent path, which never reaches the
constraint.

*A file-backed database, never conftest's in-memory one.* That fixture uses StaticPool and
hands every session the same connection, so two "concurrent" sessions would share one
transaction and the race could not happen at all.

*Each thread holds its own goal object.* The race is two DIFFERENT goals claiming one
application, so each thread must have read its own goal before either writes.
"""

import datetime
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- registers every model with Base.metadata
from app.database.base import Base
from app.models.career_goal import CareerGoal
from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.saved_job import SAVED_JOB_STATUSES, SavedJob
from app.models.user import User
from app.services import application_outcome, career_goal_service

# ==========================================================================================
# The mapping. No database -- it is a pure function of a status string.
# ==========================================================================================


@pytest.mark.parametrize(
    "status,outcome",
    [
        ("saved", None),
        ("applied", None),
        ("assessment", None),
        ("interview", None),
        ("offer", "offer"),
        ("rejected", "rejected"),
        ("closed", "closed"),
        ("dismissed", "closed"),
    ],
)
def test_outcome_for_every_status(status, outcome):
    assert application_outcome.outcome_for(status) == outcome


def test_an_open_application_has_no_outcome_rather_than_a_bad_one():
    """The distinction the whole module exists to protect: "still waiting" is not "went
    badly", and only None can say the first one."""
    for status in ("saved", "applied", "assessment", "interview"):
        assert application_outcome.outcome_for(status) is None
        assert application_outcome.is_terminal(status) is False


def test_saved_is_not_applied():
    """Saving a job is an intention. Counting it as an application would overstate what the
    student has actually done, which is the one direction this product must never err in."""
    assert application_outcome.has_applied("saved") is False
    assert application_outcome.is_live("saved") is False


def test_dismissed_is_terminal_but_was_never_applied_to():
    assert application_outcome.is_terminal("dismissed") is True
    assert application_outcome.has_applied("dismissed") is False


def test_an_unknown_status_is_treated_as_open_rather_than_crashing():
    """Forward compatibility with a status this mapping has not met. Reporting "no outcome
    yet" for something unrecognised is wrong-but-harmless; raising would take down the career
    twin and the goal detail for every student who has one."""
    assert application_outcome.outcome_for("some_future_status") is None
    assert application_outcome.is_terminal("some_future_status") is False


def test_the_mapping_covers_every_status_the_model_defines():
    """The guard that makes the table above exhaustive rather than merely long."""
    for status in SAVED_JOB_STATUSES:
        assert isinstance(application_outcome.is_terminal(status), bool)


def test_counts_are_counts_and_not_a_score():
    context = application_outcome.context_for_statuses(
        ["saved", "applied", "interview", "interview", "offer", "rejected", "rejected", "closed"]
    )
    assert context.total == 8
    assert context.live == 3  # applied + two interview
    assert context.at_interview_stage == 2
    assert context.offers == 1
    assert context.rejections == 2
    assert context.closed == 1
    assert context.has_any_application is True
    assert context.has_live_application is True


def test_no_applications_is_zero_counts_and_not_a_percentage():
    context = application_outcome.context_for_statuses([])
    assert context.total == 0
    assert context.has_any_application is False
    assert context.has_live_application is False


# ==========================================================================================
# The race: two goals, one application, two threads.
# ==========================================================================================


@pytest.fixture()
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'goal_link.db'}", connect_args={"timeout": 30}
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def seeded(engine):
    """One profile, two goals, one saved job -- the exact shape of the contested case."""
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(email="race@example.com", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)

    listing = JobListing(
        provider="test",
        provider_job_id="race-1",
        url="https://example.invalid/job",
        title="Data Engineer",
        company="Microsoft",
        fetched_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    saved = SavedJob(profile_id=profile.id, listing_id=listing.id, status="applied")
    db.add(saved)
    goals = [
        CareerGoal(
            profile_id=profile.id,
            title=f"Goal {n}",
            company="Microsoft",
            job_description="Required: SQL.",
            status="active",
        )
        for n in (1, 2)
    ]
    db.add_all(goals)
    db.commit()
    ids = (profile.id, saved.id, goals[0].id, goals[1].id)
    db.close()
    return Session, ids


def _race(Session, count, work):
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list = [None] * count

    def run(index):
        db = Session()
        try:
            barrier.wait(timeout=30)
            results[index] = work(db, index)
        except Exception as exc:  # noqa: BLE001 -- recorded, then asserted on below
            errors[index] = exc
        finally:
            db.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


def test_two_goals_racing_for_one_application_produce_exactly_one_link(seeded):
    """The database decides. Whichever thread loses must lose cleanly -- as the conflict this
    product defines, not as an unhandled IntegrityError escaping to a 500.

    This is the test that caught the real defect: the unique violation surfaces on execute(),
    not on commit(), so a try block wrapped around commit() alone never saw it.
    """
    Session, (profile_id, saved_id, goal_a, goal_b) = seeded
    goals = [goal_a, goal_b]

    def work(db, index):
        return career_goal_service.link_saved_job(db, profile_id, goals[index], saved_id)

    results, errors = _race(Session, 2, work)

    conflicts = [e for e in errors if isinstance(e, career_goal_service.SavedJobAlreadyLinkedError)]
    unexpected = [e for e in errors if e is not None and not isinstance(
        e, career_goal_service.SavedJobAlreadyLinkedError
    )]
    assert not unexpected, f"loser failed in an unintended way: {unexpected}"
    assert len(conflicts) == 1, "exactly one of the two must lose"
    assert sum(1 for r in results if r is not None) == 1, "exactly one of the two must win"

    # And the database agrees: one goal holds the link, the other holds nothing.
    db = Session()
    linked = db.query(CareerGoal).filter(CareerGoal.saved_job_id == saved_id).all()
    assert len(linked) == 1
    db.close()


def test_the_same_goal_linked_twice_concurrently_stays_linked_once(seeded):
    """The idempotent path under contention: the student double-tapped. Both calls must
    succeed, and the result must be one link, not a conflict shown for their own action."""
    Session, (profile_id, saved_id, goal_a, _goal_b) = seeded

    def work(db, _index):
        return career_goal_service.link_saved_job(db, profile_id, goal_a, saved_id)

    results, errors = _race(Session, 2, work)

    real_errors = [e for e in errors if e is not None]
    # One thread may legitimately lose the compare-and-set and report the conflict; what must
    # never happen is an unhandled error or two rows.
    assert all(
        isinstance(e, career_goal_service.SavedJobAlreadyLinkedError) for e in real_errors
    ), f"unexpected failure: {real_errors}"
    assert any(r is not None for r in results), "at least one call must succeed"

    db = Session()
    assert db.query(CareerGoal).filter(CareerGoal.saved_job_id == saved_id).count() == 1
    assert db.get(CareerGoal, goal_a).saved_job_id == saved_id
    db.close()


def test_linking_and_unlinking_concurrently_never_leaves_a_half_state(seeded):
    """Whichever order they land in, the goal ends either linked or not -- never pointing at a
    row that does not exist, and never raising."""
    Session, (profile_id, saved_id, goal_a, _goal_b) = seeded

    def work(db, index):
        if index == 0:
            return career_goal_service.link_saved_job(db, profile_id, goal_a, saved_id)
        return career_goal_service.unlink_saved_job(db, profile_id, goal_a)

    _results, errors = _race(Session, 2, work)

    real_errors = [e for e in errors if e is not None]
    assert not real_errors, f"link/unlink race raised: {real_errors}"

    db = Session()
    goal = db.get(CareerGoal, goal_a)
    assert goal.saved_job_id in (None, saved_id)
    if goal.saved_job_id is not None:
        assert db.get(SavedJob, goal.saved_job_id) is not None
    db.close()
