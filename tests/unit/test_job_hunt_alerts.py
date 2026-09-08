"""Job alert scheduling rules, in isolation from the database where possible.

The scheduling logic is where alerts either respect a student or become the thing they mute,
so quiet hours, due-ness and the catch-up rule each get their own tests rather than being
exercised incidentally through an endpoint.
"""

import datetime

import pytest

from app.models.job_alert import JobAlert
from app.services.job_hunt import alerts


def alert(**overrides) -> JobAlert:
    base = dict(
        id=1,
        profile_id=1,
        name="Remote Python",
        frequency="daily",
        hour_local=9,
        is_enabled=True,
        day_of_week=None,
        quiet_hours_start=None,
        quiet_hours_end=None,
        last_run_at=None,
    )
    base.update(overrides)
    return JobAlert(**base)


class TestQuietHours:
    def test_no_quiet_hours_means_never_quiet(self):
        assert not alerts.in_quiet_hours(alert(), 3)

    def test_a_daytime_window_is_inclusive_of_its_start_and_exclusive_of_its_end(self):
        a = alert(quiet_hours_start=13, quiet_hours_end=15)
        assert alerts.in_quiet_hours(a, 13)
        assert alerts.in_quiet_hours(a, 14)
        assert not alerts.in_quiet_hours(a, 15)
        assert not alerts.in_quiet_hours(a, 12)

    def test_a_window_that_wraps_midnight_is_one_range_not_two(self):
        # 22:00 -> 07:00 is the shape almost every student actually wants, and rejecting it
        # would push them into setting something they did not mean.
        a = alert(quiet_hours_start=22, quiet_hours_end=7)
        assert alerts.in_quiet_hours(a, 23)
        assert alerts.in_quiet_hours(a, 0)
        assert alerts.in_quiet_hours(a, 6)
        assert not alerts.in_quiet_hours(a, 7)
        assert not alerts.in_quiet_hours(a, 12)

    def test_a_zero_width_window_means_no_quiet_hours_not_always_quiet(self):
        # The other reading would silently disable an alert the student believes is running,
        # and they would have no way to tell from the UI that it had.
        a = alert(quiet_hours_start=9, quiet_hours_end=9)
        for hour in range(24):
            assert not alerts.in_quiet_hours(a, hour)


class TestIsDue:
    def _now(self, hour=10, day=8):
        # 2026-09-08 is a Tuesday (weekday 1).
        return datetime.datetime(2026, 9, day, hour, 0, 0)

    def test_a_disabled_alert_is_never_due(self):
        # Consent is the gate. Nothing below this matters if it is off.
        assert not alerts.is_due(alert(is_enabled=False), self._now())

    def test_a_daily_alert_is_due_once_its_hour_has_arrived(self):
        assert alerts.is_due(alert(hour_local=9), self._now(hour=10))

    def test_a_daily_alert_is_not_due_before_its_hour(self):
        assert not alerts.is_due(alert(hour_local=14), self._now(hour=10))

    def test_a_daily_alert_that_already_ran_today_does_not_run_again(self):
        a = alert(hour_local=9, last_run_at=datetime.datetime(2026, 9, 8, 9, 30))
        assert not alerts.is_due(a, self._now(hour=18))

    def test_a_worker_that_was_down_catches_up_once_rather_than_firing_a_burst(self):
        # Three days of missed runs must produce one notification, not three.
        a = alert(hour_local=9, last_run_at=datetime.datetime(2026, 9, 5, 9, 0))
        assert alerts.is_due(a, self._now(hour=10))

    def test_a_weekly_alert_only_runs_on_its_day(self):
        # 2026-09-08 is a Tuesday, weekday 1.
        tuesday = alert(frequency="weekly", day_of_week=1, hour_local=9)
        monday = alert(frequency="weekly", day_of_week=0, hour_local=9)
        assert alerts.is_due(tuesday, self._now(hour=10))
        assert not alerts.is_due(monday, self._now(hour=10))

    def test_a_weekly_alert_that_ran_this_week_does_not_run_again(self):
        a = alert(
            frequency="weekly",
            day_of_week=1,
            hour_local=9,
            last_run_at=datetime.datetime(2026, 9, 8, 9, 5),
        )
        assert not alerts.is_due(a, self._now(hour=18))


class TestValidation:
    def test_a_weekly_alert_without_a_day_is_rejected_rather_than_guessed(self):
        with pytest.raises(alerts.InvalidAlertError):
            alerts._validate("weekly", None, 9, None, None)

    def test_an_unknown_frequency_is_rejected(self):
        with pytest.raises(alerts.InvalidAlertError):
            alerts._validate("hourly", None, 9, None, None)

    def test_half_a_quiet_window_is_rejected(self):
        # Guessing the other half would invent a quiet period the student never asked for.
        with pytest.raises(alerts.InvalidAlertError):
            alerts._validate("daily", None, 9, 22, None)
        with pytest.raises(alerts.InvalidAlertError):
            alerts._validate("daily", None, 9, None, 7)

    def test_a_complete_quiet_window_is_accepted(self):
        alerts._validate("daily", None, 9, 22, 7)

    def test_out_of_range_hours_are_rejected(self):
        with pytest.raises(alerts.InvalidAlertError):
            alerts._validate("daily", None, 24, None, None)
        with pytest.raises(alerts.InvalidAlertError):
            alerts._validate("weekly", 7, 9, None, None)


def test_the_free_tier_gets_a_real_alert_not_zero():
    # A job product whose "tell me when something appears" loop is entirely paywalled is a
    # demo, not a free tier. Premium raises the ceiling rather than unlocking the feature.
    assert alerts.FREE_MAX_ALERTS >= 1
    assert alerts.PREMIUM_MAX_ALERTS > alerts.FREE_MAX_ALERTS


def test_a_single_run_cannot_produce_an_unbounded_pile_of_notifications():
    assert 1 <= alerts.MAX_NOTIFIED_PER_RUN <= 10
