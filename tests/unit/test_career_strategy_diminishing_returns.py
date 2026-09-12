"""Performance series analysis -- and, more importantly, its refusal to conclude.

Most of these tests are about restraint. It is easy to write a trend detector that always
returns a trend; the useful part is that this one declines below three observations, and that
a flat series is never reported as directional just because floating-point noise gave the
regression a sign.

`series_for` is pure over the rows it is handed, so the bulk of these build AssessmentResult
objects in memory without a session. The database-backed test at the bottom checks the wiring
through a real bundle.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.assessment_result import AssessmentResult
from app.services.career_strategy import diminishing_returns, signals
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    days_ago,
    make_profile,
    make_role,
)


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _results(skill: str, scores):
    """Unpersisted rows, oldest first -- series_for only reads skill_scores and created_at."""
    return [
        AssessmentResult(
            assessment_id=index,
            skill_scores={skill: score},
            overall_score=score,
            priority_weaknesses=[],
            created_at=datetime.datetime(2026, 1, 1) + datetime.timedelta(days=index),
        )
        for index, score in enumerate(scores)
    ]


class TestItRefusesToConcludeFromTooLittle:
    @pytest.mark.parametrize("scores", [[], [0.9], [0.9, 0.92]])
    def test_below_three_observations_is_insufficient_history(self, scores):
        series = diminishing_returns.series_for("SQL", _results("SQL", scores))
        assert series.verdict == "insufficient_history"
        assert series.is_sufficient is False

    def test_insufficient_history_reports_no_statistics_at_all(self):
        # Not zero, not a default -- absent. A mean of 0.0 here would read as "they score 0%".
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.9, 0.95]))
        assert series.mean is None
        assert series.variance is None
        assert series.trend is None

    def test_two_points_are_never_called_a_trend(self):
        # Any two points are perfectly linear; reporting that as "improving" would be a
        # fabricated statistical conclusion.
        rising = diminishing_returns.series_for("SQL", _results("SQL", [0.2, 0.9]))
        assert rising.verdict == "insufficient_history"

    def test_the_latest_score_is_still_reported_when_history_is_short(self):
        # We do know their most recent score; we just cannot say anything about direction.
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.4, 0.7]))
        assert series.latest == 0.7
        assert series.observation_count == 2

    def test_the_reason_says_how_many_more_are_needed(self):
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.5]))
        assert str(diminishing_returns.MIN_OBSERVATIONS) in series.reason


class TestVerdicts:
    def test_consistently_high_and_flat_is_stable_high(self):
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.92, 0.94, 0.91, 0.95]))
        assert series.verdict == "stable_high"
        assert series.is_sufficient is True
        assert series.mean > diminishing_returns.HIGH_MEAN

    def test_repeated_practice_without_movement_is_stable_low(self):
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.45, 0.47, 0.44, 0.46]))
        assert series.verdict == "stable_low"
        assert "not shifting" in series.reason

    def test_a_rising_series_is_improving(self):
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.3, 0.5, 0.7, 0.9]))
        assert series.verdict == "improving"
        assert series.trend > 0

    def test_a_falling_series_is_declining(self):
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.9, 0.7, 0.5, 0.3]))
        assert series.verdict == "declining"
        assert series.trend < 0

    def test_a_scattered_series_is_volatile_not_a_trend(self):
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.3, 0.9, 0.35, 0.85, 0.4]))
        assert series.verdict == "volatile"

    def test_stability_is_checked_before_direction(self):
        # A flat-but-strong series has a near-zero slope whose sign is noise. Calling it
        # "declining" off that sign would demote a genuine strength.
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.93, 0.92, 0.91]))
        assert series.verdict == "stable_high"

    def test_a_steady_mid_range_series_is_neither_a_strength_nor_stuck(self):
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.68, 0.7, 0.69]))
        assert series.verdict == "volatile"
        assert series.verdict != "stable_high"
        assert series.verdict != "stable_low"

    def test_every_verdict_is_in_the_declared_set(self):
        for scores in ([0.9], [0.9, 0.9, 0.9], [0.1, 0.5, 0.9], [0.9, 0.5, 0.1], [0.1, 0.9, 0.2, 0.8]):
            series = diminishing_returns.series_for("SQL", _results("SQL", scores))
            assert series.verdict in diminishing_returns.VERDICTS


class TestSeriesConstruction:
    def test_scores_are_ordered_as_given_oldest_first(self):
        series = diminishing_returns.series_for("SQL", _results("SQL", [0.3, 0.5, 0.7]))
        assert series.observations == [0.3, 0.5, 0.7]
        assert series.latest == 0.7

    def test_results_that_do_not_mention_the_skill_are_skipped_not_zeroed(self):
        rows = _results("Python", [0.9, 0.9, 0.9])
        series = diminishing_returns.series_for("SQL", rows)
        # Three Python results say nothing about SQL. Counting them as 0.0 would invent a
        # failure the student never had.
        assert series.observation_count == 0
        assert series.verdict == "insufficient_history"

    def test_skill_matching_is_token_based_not_exact(self):
        rows = _results("sql", [0.9, 0.91, 0.92])
        series = diminishing_returns.series_for("SQL", rows)
        assert series.observation_count == 3

    def test_only_one_observation_is_taken_per_result_row(self):
        rows = [
            AssessmentResult(
                assessment_id=1,
                skill_scores={"SQL": 0.9, "sql": 0.2},
                overall_score=0.5,
                priority_weaknesses=[],
                created_at=datetime.datetime(2026, 1, 1),
            )
        ]
        # One sitting is one observation, even when the payload happens to hold two keys that
        # both match -- otherwise a single test would look like a trend.
        assert diminishing_returns.series_for("SQL", rows).observation_count == 1

    def test_a_missing_skill_scores_payload_does_not_raise(self):
        rows = [
            AssessmentResult(
                assessment_id=1, skill_scores=None, overall_score=0.5, priority_weaknesses=[],
                created_at=datetime.datetime(2026, 1, 1),
            )
        ]
        assert diminishing_returns.series_for("SQL", rows).observation_count == 0


class TestThroughARealBundle:
    def test_analyse_covers_every_skill_in_the_universe(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0, "Python": 0.7})
        profile = make_profile(db, target_role_id=role.id)
        for index, score in enumerate([0.9, 0.92, 0.91]):
            add_assessment_result(db, profile, {"SQL": score}, created_at=days_ago(30 - index))

        bundle = signals.collect(db, profile, now=NOW)
        series = diminishing_returns.analyse(bundle)

        assert set(series) == {"SQL", "Python"}
        assert series["SQL"].verdict == "stable_high"
        # Python was never assessed. Its verdict must be "we don't know", not "they're bad".
        assert series["Python"].verdict == "insufficient_history"
        assert series["Python"].mean is None

    def test_interview_dimensions_never_appear_as_a_series(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        bundle = signals.collect(db, profile, now=NOW)
        series = diminishing_returns.analyse(bundle)
        assert not any(key.startswith("interview:") for key in series)

    def test_the_same_bundle_analysed_twice_is_identical(self, db):
        role = make_role(db, "SDE", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id)
        add_assessment_result(db, profile, {"SQL": 0.5}, created_at=days_ago(3))
        add_assessment_result(db, profile, {"SQL": 0.55}, created_at=days_ago(2))
        add_assessment_result(db, profile, {"SQL": 0.52}, created_at=days_ago(1))

        bundle = signals.collect(db, profile, now=NOW)
        first = {key: value.model_dump() for key, value in diminishing_returns.analyse(bundle).items()}
        second = {key: value.model_dump() for key, value in diminishing_returns.analyse(bundle).items()}
        assert first == second
