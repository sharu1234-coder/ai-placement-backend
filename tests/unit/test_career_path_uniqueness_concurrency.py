"""Two selections of the same career role, arriving at once (F12-03).

select_role() looks for the profile's row for this role family and creates one when it finds
none. Two overlapping selections both run that lookup before either commits, so both used to
find nothing and both inserted: two rows for one role, and progress made on one of them lost as
soon as a later selection picked the other. Measured on the unchanged backend through the real
HTTP route -- on PostgreSQL every overlapping request created its own row, on SQLite the second
request failed with "database is locked".

What decides the winner now is career_paths' uq_career_path_per_role_family, not the lookup, so
these tests assert the outcome rather than the mechanism: one row per (profile, role family),
every request answered with that same row, nothing 500ing, and the student's completed work still
attached to it.

Four things about the setup are load-bearing, three of them the ones spelled out in
tests/unit/test_job_hunt_entitlement_concurrency.py and test_interview_evaluation_concurrency.py.

*Real threads on real sessions.* Calling select_role twice in a row proves nothing: the second
call finds the first one's committed row and returns it, which is the sequential path and reaches
none of the recovery being tested.

*A file-backed database, never conftest's in-memory one.* That fixture uses StaticPool, which
hands every session the SAME connection -- two "concurrent" sessions would share one transaction,
each would see the other's uncommitted INSERT, the constraint would never fire, and these tests
would pass no matter what the code did.

*A barrier at the last point before the write.* select_role() does all its slow work before it
writes anything, so releasing every thread from there guarantees they have all completed the
"does this role already have a path?" lookup and none has started its write. That makes the race
certain rather than a matter of scheduling luck. Assigning a RacingProvider's barrier arms it;
tests/fixtures/career_path_helpers.py installs it on template_skill_explanations, which is what
F12-05 put in the position the skill-explanation AI call used to hold.

*Foreign keys on.* One test needs a real, unrelated IntegrityError, and SQLite only enforces
foreign keys when asked -- the same reason conftest turns the pragma on.

SQLite serialises writers, so what these demonstrate there is not parallel execution: it is that
the outcome is decided by the constraint rather than by a Python-side check, and therefore holds
however the transactions interleave. Run against PostgreSQL (TEST_DATABASE_URL) the same
constraint does the same job with genuine parallelism, and the recovery path additionally has to
survive a failed statement poisoning the transaction, which SQLite cannot show.
"""

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.main  # noqa: F401  -- registers every model with Base.metadata
from app.database.base import Base
from app.models.career_path import CareerPath
from app.models.career_path_phase import CareerPathPhase
from app.models.career_path_project import CareerPathProject
from app.models.career_path_skill_item import CareerPathSkillItem
from app.models.profile import StudentProfile
from app.models.user import User
from app.services.career_path import service as svc
from tests.fixtures.career_path_helpers import RacingProvider

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

CURATED_ROLE = "Data Analyst"
CURATED_FAMILY = "data_analyst"
OTHER_CURATED_ROLE = "Data Scientist"
CUSTOM_ROLE = "Quantum Data Wrangler"
CUSTOM_FAMILY = "custom:quantum_data_wrangler"


@pytest.fixture()
def engine(tmp_path):
    if _TEST_DATABASE_URL:
        # NullPool for the same reason conftest uses it: no connection may outlive the test whose
        # tables are about to be dropped, or the next drop_all waits behind its locks forever.
        engine = create_engine(
            _TEST_DATABASE_URL, poolclass=NullPool, connect_args={"options": "-c lock_timeout=15000"}
        )
        Base.metadata.drop_all(bind=engine)
    else:
        # timeout raises SQLite's own lock wait above the 5s default: several threads contending
        # for the write lock on a slow machine would otherwise surface as "database is locked",
        # which would be a property of the fixture rather than of the code under test. The point
        # of this suite is that the code no longer produces that error for a reason -- the write
        # is short -- so the fixture must not be what proves it.
        engine = create_engine(f"sqlite:///{tmp_path / 'career_path_race.db'}", connect_args={"timeout": 30})

        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        if _TEST_DATABASE_URL:
            Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def sessions(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _make_profile(sessions) -> int:
    db = sessions()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        db.add(user)
        db.commit()
        profile = StudentProfile(user_id=user.id)
        db.add(profile)
        db.commit()
        return profile.id
    finally:
        db.close()


@pytest.fixture()
def profile(sessions):
    return _make_profile(sessions)


@pytest.fixture()
def other_profile(sessions):
    return _make_profile(sessions)


def race(sessions, count, work):
    """Run `work(db, index)` in `count` threads. Whatever each returns or raises is collected in
    order, so a test can assert on both."""
    results = [None] * count
    errors = [None] * count

    def run(index):
        db = sessions()
        try:
            results[index] = work(db, index)
        except BaseException as exc:  # noqa: BLE001 -- reported, see the assertions
            errors[index] = exc
        finally:
            db.close()

    threads = [threading.Thread(target=run, args=(i,), name=f"racer-{i}") for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    assert not any(thread.is_alive() for thread in threads), "a racing thread never finished"
    return results, errors


def paths_of(sessions, profile_id, family_key=None):
    db = sessions()
    try:
        query = db.query(CareerPath).filter(CareerPath.profile_id == profile_id)
        if family_key is not None:
            query = query.filter(CareerPath.role_family_key == family_key)
        return query.order_by(CareerPath.id).all()
    finally:
        db.close()


def duplicate_groups(sessions):
    """The invariant query itself: every (profile, role family) with more than one row."""
    db = sessions()
    try:
        return db.execute(
            text(
                "SELECT profile_id, role_family_key, COUNT(*) FROM career_paths"
                " GROUP BY profile_id, role_family_key HAVING COUNT(*) > 1"
            )
        ).all()
    finally:
        db.close()


def only(errors):
    return [f"{type(e).__name__}: {e}" for e in errors if e is not None]


# --------------------------------------------------------------------------- A, B: same role
@pytest.mark.parametrize("racers", [2, 5])
def test_concurrent_selections_of_the_same_role_converge_on_one_row(sessions, profile, racers):
    """A and B of the required matrix: 2 and 5 requests for one profile's one new role.

    A brand-new custom role, so every racer takes the create path -- nothing exists for the
    lookup to find, which is the case that produced a row per request.
    """
    provider = RacingProvider(barrier=threading.Barrier(racers))

    results, errors = race(sessions, racers, lambda db, _i: svc.select_role(db, profile, provider, custom_role=CUSTOM_ROLE))

    assert only(errors) == [], "a concurrent selection failed outright"
    rows = paths_of(sessions, profile)
    assert len(rows) == 1, f"{racers} concurrent selections stored {len(rows)} rows: {[r.id for r in rows]}"
    assert duplicate_groups(sessions) == []
    assert {read.id for read in results} == {rows[0].id}, "requests were answered with different paths"
    assert rows[0].role_family_key == CUSTOM_FAMILY
    assert rows[0].is_primary is True
    # Whichever request created it, the row is complete: a partially built path would show up
    # here as a missing phase or skill item.
    assert len(rows[0].role_overview) > 0
    assert rows[0].generated_definition is not None, "a custom role's definition cache must be stored"
    db = sessions()
    try:
        assert db.query(CareerPathPhase).filter(CareerPathPhase.career_path_id == rows[0].id).count() == 5
        items = db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == rows[0].id).all()
        assert len(items) == 1, "the surviving path's skill items were written once, not once per request"
        assert items[0].why_it_matters is not None, "the surviving path kept the explanation text of the request that built it"
    finally:
        db.close()


def test_concurrent_reselection_of_an_existing_role_keeps_one_row_and_its_children(sessions, profile):
    """The same race on a role the student already has -- re-selecting from the catalog.

    Both requests find the row, and both would rebuild its children. The rebuild deletes and
    re-inserts them, so two overlapping rebuilds must not leave two sets behind.
    """
    provider = RacingProvider()
    db = sessions()
    try:
        first = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.close()
    before = len(paths_of(sessions, profile, CURATED_FAMILY)[0].role_overview)
    assert before > 0

    racing = RacingProvider(barrier=threading.Barrier(2))
    results, errors = race(sessions, 2, lambda db, _i: svc.select_role(db, profile, racing, custom_role=CURATED_ROLE))

    assert only(errors) == []
    rows = paths_of(sessions, profile)
    assert len(rows) == 1 and rows[0].id == first.id
    assert {read.id for read in results} == {first.id}
    db = sessions()
    try:
        items = db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == first.id).all()
        phases = db.query(CareerPathPhase).filter(CareerPathPhase.career_path_id == first.id).all()
        projects = db.query(CareerPathProject).filter(CareerPathProject.career_path_id == first.id).all()
    finally:
        db.close()
    assert len(items) == len(first.phases[0].skill_items) + sum(len(p.skill_items) for p in first.phases[1:])
    assert len(phases) == 5, f"two overlapping rebuilds left {len(phases)} phases"
    assert len({(i.phase_key, i.skill_key) for i in items}) == len(items), "a skill item was stored twice"
    assert len({p.project_key for p in projects}) == len(projects), "a project was stored twice"


# --------------------------------------------------------------------------- C: profile isolation
def test_two_profiles_selecting_the_same_role_get_a_row_each(sessions, profile, other_profile):
    """C: the constraint is on (profile_id, role_family_key), never on the role alone.

    If it were ever narrowed to role_family_key, one student's selection would refuse another's
    -- and the recovery would hand them the other student's path.
    """
    provider = RacingProvider(barrier=threading.Barrier(2))
    profiles = [profile, other_profile]

    results, errors = race(sessions, 2, lambda db, i: svc.select_role(db, profiles[i], provider, custom_role=CUSTOM_ROLE))

    assert only(errors) == []
    mine = paths_of(sessions, profile)
    theirs = paths_of(sessions, other_profile)
    assert len(mine) == 1 and len(theirs) == 1
    assert mine[0].id != theirs[0].id
    assert {r.id for r in results} == {mine[0].id, theirs[0].id}
    assert mine[0].profile_id == profile and theirs[0].profile_id == other_profile
    assert mine[0].is_primary is True and theirs[0].is_primary is True


# --------------------------------------------------------------------------- D: different roles
def test_concurrent_selections_of_different_roles_are_both_created(sessions, profile):
    """D: two different roles at once must both be created and both be answered.

    The constraint is on (profile_id, role_family_key), so it has nothing to say about two
    different families -- and this test is what would fail first if it were ever widened to
    profile_id alone: one of the student's two selections would be refused, and the recovery would
    answer it with the other role's path.

    Characterising the primary flag deliberately, rather than pinning a number. Both requests
    clear the flag before either has inserted its row, so neither can see the other's row to clear
    it, and both end up marked primary -- measured on PostgreSQL, before and after this phase's
    change, and equally true of the code this replaced. It is a pre-existing race in a
    service-enforced invariant (no constraint has ever backed "one primary per profile"), it is not
    F12-03, and it is not duplicate data: both rows are roles the student really did select. What it
    costs is that GET /career-path/current picks one of the two. The next selection of any role
    repairs it, because the flag is now cleared on every row. Fixing it properly means a second
    invariant and a product decision about which role wins, so it is reported, not improvised.

    What must hold on every engine is asserted: two rows, nothing refused, and a current path that
    exists. test_reselecting_two_existing_roles_at_once_leaves_one_primary covers the case where
    the rows do exist, which is the one the demotion change made coherent.
    """
    provider = RacingProvider(barrier=threading.Barrier(2))
    roles = [CURATED_ROLE, OTHER_CURATED_ROLE]

    results, errors = race(sessions, 2, lambda db, i: svc.select_role(db, profile, provider, custom_role=roles[i]))

    assert only(errors) == [], "selecting two different roles at once failed"
    rows = paths_of(sessions, profile)
    assert {r.role_family_key for r in rows} == {CURATED_FAMILY, "data_scientist"}
    assert len(rows) == 2, "a different role must not be refused by the uniqueness constraint"
    assert len(results) == 2 and all(read is not None for read in results)
    assert [r.id for r in rows] == sorted({r.id for r in rows}), "the two roles share a row"
    assert len([r for r in rows if r.is_primary]) >= 1, "neither of the student's selections became current"
    db = sessions()
    try:
        assert svc.get_current(db, profile) is not None
    finally:
        db.close()


def test_reselecting_two_existing_roles_at_once_leaves_one_primary(sessions, profile):
    """The regression test for how a selected role is made current.

    Each request used to demote "every path but mine". Two requests for different roles then
    locked each other's row and waited for the row the other held, which PostgreSQL resolves by
    failing one of them -- a 500 on an ordinary role selection. Measured directly:
    scratchpad/p15/probe_demotion.py reports psycopg2.errors.DeadlockDetected for that statement
    pair and a clean pair of commits for this one.

    Clearing the flag on every row means both requests run the identical statement, contend on the
    same rows in the same order, and take turns: the second waits for the first to commit and then
    sees its row, so exactly one path is left current.
    """
    provider = RacingProvider()
    db = sessions()
    try:
        analyst = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
        scientist = svc.select_role(db, profile, provider, custom_role=OTHER_CURATED_ROLE)
    finally:
        db.close()
    assert {r.id for r in paths_of(sessions, profile)} == {analyst.id, scientist.id}

    racing = RacingProvider(barrier=threading.Barrier(2))
    roles = [CURATED_ROLE, OTHER_CURATED_ROLE]
    results, errors = race(sessions, 2, lambda db, i: svc.select_role(db, profile, racing, custom_role=roles[i]))

    assert only(errors) == [], "re-selecting two different roles at once failed"
    rows = paths_of(sessions, profile)
    assert len(rows) == 2, "re-selection created a row instead of reusing one"
    primary = [r for r in rows if r.is_primary]
    assert len(primary) == 1, f"primary flags: {[(r.role_family_key, r.is_primary) for r in rows]}"
    assert {read.id for read in results} == {analyst.id, scientist.id}
    db = sessions()
    try:
        current = svc.get_current(db, profile)
    finally:
        db.close()
    assert current is not None and current.id == primary[0].id


# --------------------------------------------------------------------------- E, F: sequential
def test_selecting_the_same_role_twice_in_a_row_reuses_the_row(sessions, profile):
    """E and F: the sequential path, unchanged by any of this -- the second selection finds the
    committed row and refreshes it rather than adding a second."""
    provider = RacingProvider()
    db = sessions()
    try:
        first = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.close()
    db = sessions()
    try:
        second = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.close()

    assert second.id == first.id
    assert len(paths_of(sessions, profile)) == 1
    assert duplicate_groups(sessions) == []


def test_a_selection_that_fails_before_committing_leaves_the_role_selectable(sessions, profile, monkeypatch):
    """G: a request that dies mid-generation must not reserve the role.

    Nothing is written before the AI work completes, so there is nothing to roll back and no
    half-built row for the next request to adopt -- it creates the path as if the first had never
    happened. The failure is injected at the one point that writes, so the transaction really is
    open when it happens.
    """
    provider = RacingProvider()
    real = svc._sync_path_children

    def explode(*args, **kwargs):
        raise RuntimeError("generation died mid-write")

    monkeypatch.setattr(svc, "_sync_path_children", explode)
    db = sessions()
    try:
        with pytest.raises(RuntimeError):
            svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.rollback()
        db.close()
    assert paths_of(sessions, profile) == [], "the failed request left a row behind"

    monkeypatch.setattr(svc, "_sync_path_children", real)
    db = sessions()
    try:
        retried = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.close()

    rows = paths_of(sessions, profile)
    assert len(rows) == 1 and rows[0].id == retried.id
    assert rows[0].is_primary is True
    db = sessions()
    try:
        assert db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == retried.id).count() > 0
    finally:
        db.close()


# --------------------------------------------------------------------------- H: conflict recovery
def _blind_the_locked_lookup(monkeypatch):
    """Make the lookup select_role() runs inside its write transaction miss exactly once.

    The lookup before that one is left alone, so the request behaves exactly like one whose
    checks all ran before another request committed: it proceeds to INSERT, the constraint refuses
    it, and the recovery in _insert_or_adopt runs. Reproducing the window rather than racing for
    it is what makes this deterministic -- on SQLite a genuine second connection cannot commit
    while this session holds the write lock, so the window has to be recreated. Same technique,
    and the same reason, as _blind_once in tests/unit/test_job_hunt_concurrency.py.
    """
    real = svc._find_path
    state = {"blinded": False}

    def blinded(db, profile_id, family_key, lock=False):
        if lock and not state["blinded"]:
            state["blinded"] = True
            return None
        return real(db, profile_id, family_key, lock=lock)

    monkeypatch.setattr(svc, "_find_path", blinded)
    return state


def test_a_refused_insert_recovers_the_existing_row_and_returns_normally(sessions, profile, monkeypatch):
    """H: the unique conflict itself -- the session survives it, the winner's row is loaded
    through it, and the caller gets an ordinary result.

    On PostgreSQL this is the test that matters most: a failed statement makes every later one in
    that transaction fail too, so a recovery that merely looks right on SQLite can still be broken
    in production. The savepoint is what allows the SELECT that follows.
    """
    provider = RacingProvider()
    db = sessions()
    try:
        first = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.close()
    db = sessions()
    try:
        item = db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == first.id).first()
        item.status = "completed"
        item.completed_via = "manual"
        db.commit()
        completed_key = item.skill_key
    finally:
        db.close()

    state = _blind_the_locked_lookup(monkeypatch)
    db = sessions()
    try:
        recovered = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.close()

    assert state["blinded"], "the conflict was never provoked -- this test proved nothing"
    assert recovered.id == first.id, "the refused request returned a different path than the stored one"
    rows = paths_of(sessions, profile)
    assert len(rows) == 1
    assert rows[0].is_primary is True, "the adopted path must still become the current one"
    # The adopted row is the one the earlier request built, progress included: the refused
    # request did not rebuild it, which is what would have reset this.
    statuses = {i.skill_key: i.status for phase in recovered.phases for i in phase.skill_items}
    assert statuses[completed_key] == "completed"


def test_an_unrelated_integrity_error_is_not_swallowed(sessions, monkeypatch):
    """I: only uq_career_path_per_role_family is recovered from.

    A selection for a profile that does not exist violates career_paths' foreign key to
    student_profiles. If the recovery caught IntegrityError broadly it would swallow that, find no
    row, and -- depending on how it then failed -- either raise something misleading or hand back
    someone else's path. It must surface as the error it is.
    """
    provider = RacingProvider()
    missing_profile = 987654
    db = sessions()
    try:
        with pytest.raises(IntegrityError):
            svc.select_role(db, missing_profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.rollback()
        db.close()


def test_the_conflict_predicate_tells_the_two_errors_apart(sessions, profile):
    """The discrimination in I, measured against the errors this engine really produces rather
    than against a hand-built exception.

    Both branches matter. A predicate too narrow would let the race 500; one too wide would hide
    a genuine constraint failure behind a plausible-looking career path.
    """
    provider = RacingProvider()
    db = sessions()
    try:
        existing = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.close()

    db = sessions()
    try:
        with pytest.raises(IntegrityError) as duplicate:
            db.add(
                CareerPath(
                    profile_id=profile,
                    role_family_key=CURATED_FAMILY,
                    display_role_name="Data Analyst",
                    is_primary=False,
                    generated_via="catalog",
                    role_overview={},
                )
            )
            db.flush()
        assert svc._is_role_family_conflict(duplicate.value) is True
    finally:
        db.rollback()
        db.close()

    db = sessions()
    try:
        with pytest.raises(IntegrityError) as foreign_key:
            db.add(
                CareerPath(
                    profile_id=987654,
                    role_family_key="sde",
                    display_role_name="SDE",
                    is_primary=False,
                    generated_via="catalog",
                    role_overview={},
                )
            )
            db.flush()
        assert svc._is_role_family_conflict(foreign_key.value) is False, "a foreign-key failure was mistaken for the race"
    finally:
        db.rollback()
        db.close()

    db = sessions()
    try:
        with pytest.raises(IntegrityError) as not_null:
            db.add(
                CareerPath(
                    profile_id=profile,
                    role_family_key=None,
                    display_role_name="No family",
                    is_primary=False,
                    generated_via="catalog",
                    role_overview={},
                )
            )
            db.flush()
        assert svc._is_role_family_conflict(not_null.value) is False, "a NOT NULL failure was mistaken for the race"
    finally:
        db.rollback()
        db.close()

    assert len(paths_of(sessions, profile)) == 1 and paths_of(sessions, profile)[0].id == existing.id


# --------------------------------------------------------------------------- progress preservation
def test_completed_progress_survives_a_concurrent_reselection(sessions, profile):
    """The failure the audit described, end to end: complete a skill, select the role again while
    another selection of it is in flight, and read the path back.

    Before the constraint this is where the student's work appeared to vanish -- the second
    selection created a second row, the next read picked it, and the completed skill was on the
    other one, still stored and no longer reachable.
    """
    provider = RacingProvider()
    db = sessions()
    try:
        first = svc.select_role(db, profile, provider, custom_role=CURATED_ROLE)
    finally:
        db.close()
    db = sessions()
    try:
        item = db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == first.id).first()
        item.status = "completed"
        item.completed_via = "assessment"
        db.commit()
        completed_key = item.skill_key
        project = db.query(CareerPathProject).filter(CareerPathProject.career_path_id == first.id).first()
        project.status = "completed"
        db.commit()
        completed_project = project.project_key
    finally:
        db.close()

    racing = RacingProvider(barrier=threading.Barrier(2))
    results, errors = race(sessions, 2, lambda db, _i: svc.select_role(db, profile, racing, custom_role=CURATED_ROLE))

    assert only(errors) == []
    assert {read.id for read in results} == {first.id}
    db = sessions()
    try:
        current = svc.get_current(db, profile)
    finally:
        db.close()
    assert current is not None and current.id == first.id, "the current path changed identity"
    statuses = {i.skill_key: (i.status, i.completed_via) for phase in current.phases for i in phase.skill_items}
    assert statuses[completed_key] == ("completed", "assessment"), f"{completed_key} came back as {statuses[completed_key]}"
    projects = {p.project_key: p.status for phase in current.phases for p in phase.projects}
    assert projects[completed_project] == "completed"
    assert len(paths_of(sessions, profile)) == 1
