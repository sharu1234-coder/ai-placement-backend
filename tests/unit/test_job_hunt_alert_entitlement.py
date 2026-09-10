"""Alerts are a standing search, so who is allowed to run one.

An alert is a saved search that runs by itself on a schedule. For a student whose plan is one
search in total, that is not a notification feature -- it is unlimited Job Hunt with a timer
attached, reachable in two ordinary requests: save a search, enable an alert on it. So a
background run requires an active subscription.

Three properties are checked here, and the second and third matter as much as the first:

  * a free student's alert does not run, and does not reach the provider;
  * it is *recorded* as not having run, rather than silently doing nothing -- an alert that
    just goes quiet is indistinguishable from an empty job market, and the student would draw
    the wrong conclusion from our silence;
  * being skipped costs nothing. The free search is still there, and the alert itself is not
    disabled or deleted -- it starts working the moment they subscribe.

The check lives in run_alert rather than in create_alert on purpose, and the last class pins
that: entitlement is read when work would happen, so a subscription starting or lapsing takes
effect on the very next run instead of being decided by whenever the alert was set up.
"""

import datetime
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401  -- registers every model with Base.metadata
from app.database.base import Base
from app.models.job_alert import ALERT_RUN_STATUSES, JobAlert, JobAlertDelivery
from app.models.notification_event import NotificationEvent
from app.models.profile import StudentProfile
from app.models.user import User
from app.services.job_hunt import alerts, service
from app.services.job_hunt.providers.base import ProviderJob, ProviderSearchResult
from tests.fixtures.premium_helpers import expire_premium, grant_premium

NOW = datetime.datetime(2026, 9, 10, 12, 0, 0)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class CountingProvider:
    """Records whether it was asked anything at all.

    That is the assertion that matters most here: a gate that let the provider be called and
    then discarded the result would still spend the deployment's quota on a student who is not
    entitled to it.
    """

    name = "counting"
    attribution_text = "Jobs by Counting"
    attribution_url = None
    cache_ttl = datetime.timedelta(hours=6)
    daily_call_budget = None

    def __init__(self):
        self.calls = 0

    def search(self, query):
        self.calls += 1
        return ProviderSearchResult(
            jobs=[
                ProviderJob(
                    provider=self.name,
                    provider_job_id=f"{uuid.uuid4().hex[:10]}",
                    url=f"https://jobs.example.com/{uuid.uuid4().hex[:10]}",
                    title="Data Engineer",
                    company="Acme",
                    location="Bengaluru",
                    posted_at=NOW,
                )
            ],
            total_available=1,
        )


def make_student(db, *, subscribed: bool):
    email = f"{uuid.uuid4().hex}@example.com"
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    search_profile = service.get_or_create_search_profile(db, profile.id)
    search_profile.target_role_title = "Data Engineer"
    db.commit()
    if subscribed:
        grant_premium(db, email)
    return profile, email


def make_alert(db, profile) -> JobAlert:
    alert = JobAlert(profile_id=profile.id, name="Bengaluru data roles", is_enabled=True)
    db.add(alert)
    db.commit()
    return alert


class TestAFreeStudentsAlertDoesNotRun:
    def test_the_run_is_refused(self, db):
        profile, _email = make_student(db, subscribed=False)
        alert = make_alert(db, profile)

        status, count = alerts.run_alert(db, alert, profile, CountingProvider(), now=NOW)

        assert (status, count) == ("requires_premium", 0)

    def test_the_provider_is_never_called(self, db):
        """The gate is before the search, not after it. Calling and discarding would spend the
        deployment's provider quota on a run that produces nothing."""
        profile, _email = make_student(db, subscribed=False)
        alert = make_alert(db, profile)
        provider = CountingProvider()

        alerts.run_alert(db, alert, profile, provider, now=NOW)

        assert provider.calls == 0

    def test_nothing_is_delivered_and_nothing_is_notified(self, db):
        profile, _email = make_student(db, subscribed=False)
        alert = make_alert(db, profile)

        alerts.run_alert(db, alert, profile, CountingProvider(), now=NOW)

        assert db.query(JobAlertDelivery).count() == 0
        assert db.query(NotificationEvent).count() == 0

    def test_the_refusal_is_recorded_rather_than_left_as_silence(self, db):
        """An alert that goes quiet looks exactly like a job market with nothing in it. The
        student is told which one this is."""
        profile, _email = make_student(db, subscribed=False)
        alert = make_alert(db, profile)

        alerts.run_alert(db, alert, profile, CountingProvider(), now=NOW)

        db.refresh(alert)
        assert alert.last_run_status == "requires_premium"
        assert alert.last_run_at == NOW
        # Not an error state: nothing is broken and nothing needs fixing.
        assert alert.last_error_at is None

    def test_requires_premium_is_a_recognised_status(self):
        """It travels to the client through a Literal on JobAlertRead, so an unlisted value
        would be a 500 from the response model rather than a message on a screen."""
        assert "requires_premium" in ALERT_RUN_STATUSES

    def test_the_alert_is_not_disabled_or_deleted(self, db):
        """A skipped run is not a punishment. The alert stays exactly as configured, so it
        starts working the moment they subscribe -- and so a free student can still see what
        they would be buying."""
        profile, _email = make_student(db, subscribed=False)
        alert = make_alert(db, profile)

        alerts.run_alert(db, alert, profile, CountingProvider(), now=NOW)

        db.refresh(alert)
        assert alert.is_enabled is True
        assert db.query(JobAlert).count() == 1

    def test_a_free_student_may_still_create_one(self, db):
        """The free tier keeps an alert it can create and understand. What it does not get is
        a search that runs on its own."""
        profile, _email = make_student(db, subscribed=False)

        created = alerts.create_alert(db, profile.id, name="Bengaluru data roles")

        assert created.id is not None
        assert alerts.max_alerts_for(db, profile.id) == alerts.FREE_MAX_ALERTS

    def test_being_skipped_does_not_touch_the_free_search(self, db):
        """The alert path never claims the Job Hunt allowance, in either direction: it does
        not spend it, and being refused does not consume it either."""
        from app.models.job_hunt_usage import JobHuntUsage

        profile, _email = make_student(db, subscribed=False)
        alert = make_alert(db, profile)

        for day in range(5):
            alerts.run_alert(
                db, alert, profile, CountingProvider(), now=NOW + datetime.timedelta(days=day)
            )

        assert db.query(JobHuntUsage).count() == 0


class TestASubscribersAlertRunsNormally:
    def test_it_searches_and_delivers(self, db):
        profile, _email = make_student(db, subscribed=True)
        alert = make_alert(db, profile)
        provider = CountingProvider()

        status, count = alerts.run_alert(db, alert, profile, provider, now=NOW)

        assert status == "success"
        assert count == 1
        assert provider.calls == 1
        assert db.query(JobAlertDelivery).count() == 1

    def test_a_background_run_does_not_spend_the_free_search(self, db):
        """Alerts and the interactive search draw on different things -- the subscription and
        the one free operation respectively. A background run that quietly consumed the free
        search would take it from a student who never tapped anything."""
        from app.models.job_hunt_usage import JobHuntUsage

        profile, _email = make_student(db, subscribed=True)
        alert = make_alert(db, profile)

        alerts.run_alert(db, alert, profile, CountingProvider(), now=NOW)

        assert db.query(JobHuntUsage).count() == 0


class TestEntitlementIsReadWhenTheWorkWouldHappen:
    def test_a_lapsed_subscription_stops_the_next_run(self, db):
        """Checked at run time, not at creation time. An alert set up while subscribed must
        not keep running forever after the subscription ends."""
        profile, email = make_student(db, subscribed=True)
        alert = make_alert(db, profile)
        provider = CountingProvider()
        assert alerts.run_alert(db, alert, profile, provider, now=NOW)[0] == "success"

        expire_premium(db, email)

        status, _count = alerts.run_alert(
            db, alert, profile, provider, now=NOW + datetime.timedelta(days=1)
        )

        assert status == "requires_premium"
        assert provider.calls == 1, "no further provider call after the lapse"

    def test_subscribing_starts_it_running_without_touching_the_alert(self, db):
        """The other direction, and the reason a skipped alert is left enabled: nothing has to
        be re-enabled, re-saved or re-created when the student pays."""
        profile, email = make_student(db, subscribed=False)
        alert = make_alert(db, profile)
        provider = CountingProvider()
        assert alerts.run_alert(db, alert, profile, provider, now=NOW)[0] == "requires_premium"

        grant_premium(db, email)

        status, count = alerts.run_alert(
            db, alert, profile, provider, now=NOW + datetime.timedelta(days=1)
        )

        assert (status, count) == ("success", 1)
        assert provider.calls == 1
