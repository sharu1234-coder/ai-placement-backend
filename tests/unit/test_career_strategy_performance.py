"""Query-count regression: the strategy pipeline must not scale with the size of a profile.

Career Strategy reads from a dozen tables and composes three services. That is exactly the shape
that grows an N+1 quietly -- someone adds a per-skill evidence lookup or a per-job match fetch,
every test still passes, and the endpoint degrades in production for precisely the students who
have used the app most. Nothing in a normal test suite notices, because a fixture profile has
three skills.

So these tests assert the *property* rather than a magic number: the same profile shape at 3, 10
and 25 skills, and 2, 10 and 30 tracked jobs, must issue the identical number of SELECTs. An
exact-count assertion would be brittle against any legitimate change and would teach whoever
hits it to bump the number; flatness cannot be satisfied that way -- the only way to make a
per-skill query pass is to remove it.

A generous ceiling is asserted alongside, purely to catch a change that keeps the shape flat
while multiplying the constant. It is deliberately loose: this file is about growth, not about
freezing today's implementation.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.services.career_strategy import service as strategy_service
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_interview_evaluation,
    add_job_preferences,
    add_project_build,
    add_resume,
    add_tracked_job,
    add_weakness_record,
    days_ago,
    make_profile,
    make_role,
)

# Loose on purpose -- see the module docstring. Flatness is the real assertion.
MAX_SELECTS_PER_STRATEGY_READ = 140
MAX_SELECTS_PER_JOB_READ = 160


def _counting_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    counter = {"n": 0, "on": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if counter["on"] and statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    return sessionmaker(bind=engine)(), counter


def _build(db, *, skills: int, jobs: int):
    """A realistic profile at an arbitrary size: every signal source populated."""
    role = make_role(db, "Data Engineer", {f"Skill{i}": 1.0 for i in range(skills)})
    profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                           graduation_year=2026, experience_level="fresher")
    for i in range(skills):
        add_assessment_result(db, profile, {f"Skill{i}": 0.8}, created_at=days_ago(3))
        add_project_build(db, profile, title=f"Project {i}", stack_summary=f"Skill{i}")
    add_resume(db, profile, skills=[f"skill{i}" for i in range(skills)])
    add_job_preferences(db, profile, normalized_skills=[f"skill{i}" for i in range(skills)])
    add_interview_evaluation(db, profile, clarity=0.3)
    add_weakness_record(db, profile, "interview:clarity", occurrences=3)
    last = None
    for j in range(jobs):
        last = add_tracked_job(db, profile, missing_skills=[f"Skill{j % max(skills, 1)}"],
                               matched_skills=[], listing_id_seed=j)
    return profile, last


SIZES = [(3, 2), (10, 10), (25, 30)]


@pytest.fixture()
def strategy_counts():
    """SELECTs for one steady-state strategy read, at each profile size."""
    counts = {}
    for skills, jobs in SIZES:
        db, counter = _counting_session()
        profile, _listing = _build(db, skills=skills, jobs=jobs)
        strategy_service.compute(db, profile, now=NOW)  # first read writes a snapshot
        counter["on"] = True
        strategy_service.compute(db, profile, now=NOW)  # steady state: no write
        counts[(skills, jobs)] = counter["n"]
        db.close()
    return counts


def test_a_strategy_read_does_not_scale_with_profile_size(strategy_counts):
    """The headline property. A student with 25 skills and 30 tracked jobs costs the database
    exactly what a student with 3 skills and 2 jobs costs."""
    distinct = set(strategy_counts.values())
    assert len(distinct) == 1, f"query count grows with profile size: {strategy_counts}"


def test_a_strategy_read_stays_within_a_sane_ceiling(strategy_counts):
    for size, count in strategy_counts.items():
        assert count <= MAX_SELECTS_PER_STRATEGY_READ, f"{size} issued {count} SELECTs"


def test_a_job_strategy_read_does_not_scale_with_tracked_jobs():
    """The job endpoint composes the whole career pipeline plus one listing. Neither half may
    start costing more as the student's tracker fills up."""
    counts = {}
    for skills, jobs in SIZES:
        db, counter = _counting_session()
        profile, listing = _build(db, skills=skills, jobs=jobs)
        strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        counter["on"] = True
        strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        counts[(skills, jobs)] = counter["n"]
        db.close()

    assert len(set(counts.values())) == 1, f"job strategy grows with profile size: {counts}"
    for size, count in counts.items():
        assert count <= MAX_SELECTS_PER_JOB_READ, f"{size} issued {count} SELECTs"


def test_history_is_rendered_without_a_query_per_entry():
    """Each history row carries its own change lines. If that regressed into a per-row diff
    fetch, this count would climb with the number of recorded strategies."""
    db, counter = _counting_session()
    profile, _listing = _build(db, skills=6, jobs=4)

    # Force several genuine strategy changes so history has real depth.
    for index in range(5):
        add_assessment_result(db, profile, {f"Skill{index}": 0.95}, created_at=days_ago(2))
        strategy_service.compute(db, profile, now=NOW)

    counter["on"] = True
    strategy_service.get_history(db, profile.id, limit=1)
    one = counter["n"]
    counter["n"] = 0
    strategy_service.get_history(db, profile.id, limit=20)
    many = counter["n"]
    db.close()

    assert one == many, f"history cost grows with entry count: 1 row={one}, 20 rows={many}"


def test_a_target_comparison_costs_a_bounded_amount_per_target():
    """Comparison genuinely costs one signal pass per target -- that is the price of running the
    real engine rather than approximating it. What must not happen is the *per-target* cost
    growing with profile size, which would multiply out.
    """
    from app.services.career_strategy import targets

    counts = {}
    for skills, jobs in SIZES:
        db, counter = _counting_session()
        profile, _listing = _build(db, skills=skills, jobs=jobs)
        other = make_role(db, "Data Analyst", {"Excel": 1.0, "Statistics": 1.0})
        targets.set_targets(db, profile.id, [other.id])
        strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        counter["on"] = True
        strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        counts[(skills, jobs)] = counter["n"]
        db.close()

    assert len(set(counts.values())) == 1, f"comparison grows with profile size: {counts}"
