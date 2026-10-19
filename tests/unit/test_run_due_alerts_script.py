"""The alert runner: selection, isolation, and the guarantee that one bad row cannot stop a batch.

Alerts were configurable long before anything could run them, and the gap was not in the alert
logic -- `is_due` and `run_alert` were both written and tested. What was missing was a process
that iterates alerts across every profile and calls them, which is the whole of
`scripts/run_due_alerts.py`.

So these tests are about the batch, not about alert behaviour: which alerts get picked, in what
order, and what happens when one of them misbehaves. The alert rules themselves are covered by
`test_job_hunt_alerts.py` and are deliberately not re-asserted here -- a second copy of those
expectations would be free to drift from the real ones.

The isolation test is the one that matters most in production. A nightly batch that aborts on
its first unexpected error delivers nothing to anyone else that night, and the failure would be
invisible until students asked why their alerts went quiet.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.job_alert import JobAlert
from app.models.job_saved_search import JobSavedSearch
from scripts import run_due_alerts
from tests.fixtures.career_strategy_helpers import make_profile, make_role

NOW = datetime.datetime(2026, 9, 12, 18, 0, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    # main() closes the session it is given, so a test that needs to read state afterwards
    # opens its own through this factory rather than refreshing a now-detached instance.
    session.test_factory = factory
    yield session
    session.close()


def _profile(db, user_id=1, role_name="Backend Engineer"):
    role = make_role(db, role_name, {"Python": 1.0})
    return make_profile(db, user_id=user_id, target_role_id=role.id, degree="B.Tech",
                        branch="CSE", graduation_year=2026, experience_level="fresher")


def _alert(db, profile, *, enabled=True, hour_local=9, last_run_at=None, frequency="daily"):
    search = JobSavedSearch(profile_id=profile.id, name="Backend, Bengaluru", filters={"q": "backend"})
    db.add(search)
    db.commit()
    db.refresh(search)
    alert = JobAlert(
        profile_id=profile.id,
        saved_search_id=search.id,
        name="Backend, Bengaluru",
        is_enabled=enabled,
        frequency=frequency,
        hour_local=hour_local,
        last_run_at=last_run_at,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


class TestSelection:
    def test_a_disabled_alert_is_never_selected(self, db):
        profile = _profile(db)
        _alert(db, profile, enabled=False)
        assert run_due_alerts._due_alerts(db, NOW, 0) == []

    def test_an_enabled_due_alert_is_selected(self, db):
        profile = _profile(db)
        alert = _alert(db, profile, hour_local=9)
        due = run_due_alerts._due_alerts(db, NOW, 0)
        assert [item.id for item in due] == [alert.id]

    def test_an_alert_that_already_ran_recently_is_not_selected_again(self, db):
        """`run_alert` stamps last_run_at before doing any work, which is what makes a second
        invocation -- a double-firing cron, or an overlapping run -- a no-op rather than a
        duplicate delivery."""
        profile = _profile(db)
        _alert(db, profile, hour_local=9, last_run_at=NOW - datetime.timedelta(hours=1))
        assert run_due_alerts._due_alerts(db, NOW, 0) == []

    def test_the_longest_waiting_alert_goes_first(self, db):
        """Ordering is what stops a --limit batch starving the same alerts every night."""
        a = _profile(db, user_id=1, role_name="Role A")
        b = _profile(db, user_id=2, role_name="Role B")
        recent = _alert(db, a, hour_local=9, last_run_at=NOW - datetime.timedelta(days=3))
        never = _alert(db, b, hour_local=9, last_run_at=None)

        due = run_due_alerts._due_alerts(db, NOW, 0)
        assert [item.id for item in due] == [never.id, recent.id]

    def test_the_limit_caps_the_batch(self, db):
        a = _profile(db, user_id=1, role_name="Role A")
        b = _profile(db, user_id=2, role_name="Role B")
        _alert(db, a, hour_local=9)
        _alert(db, b, hour_local=9)
        assert len(run_due_alerts._due_alerts(db, NOW, 1)) == 1


class TestBatchIsolation:
    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch):
        """Pin the batch's clock, because every test in this class depends on one.

        `main()` calls `_utcnow()` itself rather than taking a time, so unlike the
        `_due_alerts` tests above -- which pass `NOW` explicitly -- these ran against the
        machine's real clock. `alerts.is_due` returns False while `now.hour <
        alert.hour_local`, and every alert seeded here uses the helper's default
        `hour_local=9`, so nothing in this class was due unless the suite happened to run at
        or after 09:00 **UTC**.

        That was not a theoretical window. A full run on 2026-10-18 at 07:18-08:33 UTC failed
        `test_one_failing_alert_does_not_stop_the_others` with `0 == 1`, while the run before
        it at 21:19-22:29 UTC passed -- the same code, the same assertions, a different hour
        of the day.

        Freezing the clock makes these assertions stronger, not weaker. The failure above was
        the loud half; the quiet half is that
        `test_an_alert_whose_profile_vanished_is_skipped_not_fatal` and
        `test_a_dry_run_changes_nothing` both assert that `run_alert` is *never* called, which
        an empty due-list satisfies for the wrong reason. Before this fixture they passed
        vacuously for most of the day without exercising the paths they name.

        NOW is 18:00, comfortably past the seeded hour, so "due" is decided by the rule under
        test rather than by when the suite was started.
        """
        monkeypatch.setattr(run_due_alerts, "_utcnow", lambda: NOW)

    def test_one_failing_alert_does_not_stop_the_others(self, db, monkeypatch):
        """The production-critical property. A batch that aborts on its first unexpected error
        silently delivers nothing to everyone else that night."""
        a = _profile(db, user_id=1, role_name="Role A")
        b = _profile(db, user_id=2, role_name="Role B")
        first = _alert(db, a, hour_local=9)
        _alert(db, b, hour_local=9)

        ran = []

        def fake_run_alert(session, alert, profile, provider, now=None):
            if alert.id == first.id:
                raise RuntimeError("provider exploded in a way run_alert does not handle")
            ran.append(alert.id)
            return ("ok", 0)

        monkeypatch.setattr(run_due_alerts.alerts, "run_alert", fake_run_alert)
        monkeypatch.setattr(run_due_alerts, "SessionLocal", lambda: db)
        monkeypatch.setattr(run_due_alerts, "get_job_provider", lambda: object())

        assert run_due_alerts.main([]) == 0
        assert len(ran) == 1, "the second alert should still have run"

    def test_an_alert_whose_profile_vanished_is_skipped_not_fatal(self, db, monkeypatch):
        profile = _profile(db)
        alert = _alert(db, profile, hour_local=9)
        alert.profile_id = 9999  # profile deleted between selection and run
        db.commit()

        monkeypatch.setattr(run_due_alerts, "SessionLocal", lambda: db)
        monkeypatch.setattr(run_due_alerts, "get_job_provider", lambda: object())
        monkeypatch.setattr(
            run_due_alerts.alerts, "run_alert",
            lambda *a, **k: pytest.fail("must not run an alert with no profile"),
        )

        assert run_due_alerts.main([]) == 0

    def test_a_dry_run_changes_nothing(self, db, monkeypatch):
        profile = _profile(db)
        alert = _alert(db, profile, hour_local=9)
        before = alert.last_run_at

        monkeypatch.setattr(run_due_alerts, "SessionLocal", lambda: db)
        monkeypatch.setattr(
            run_due_alerts.alerts, "run_alert",
            lambda *a, **k: pytest.fail("a dry run must not run anything"),
        )

        assert run_due_alerts.main(["--dry-run"]) == 0

        fresh = db.test_factory()
        reloaded = fresh.get(JobAlert, alert.id)
        assert reloaded.last_run_at == before
        fresh.close()

    def test_a_database_that_cannot_be_reached_exits_nonzero(self, monkeypatch):
        """Exit 1 is the signal a scheduler should alert on. Exit 0 with nothing done would let
        a broken batch look like a quiet night, forever."""
        def explode():
            raise RuntimeError("database unreachable")

        monkeypatch.setattr(run_due_alerts, "SessionLocal", explode)
        assert run_due_alerts.main([]) == 1
