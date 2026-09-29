"""Multi-target strategy: one engine, several targets.

The point of every test here is that a comparison target is not a special case. It is the same
pipeline with the role context swapped, so the properties proved elsewhere in this package --
deterministic, no fabricated numbers, a proven skill produces no work -- have to hold for a
hypothetical target exactly as they do for the real one. Where a test asserts "same as", that
is the claim being defended: that there is only one engine to be wrong.

Two behaviours are load-bearing and easy to lose in a refactor. Comparing roles must never write
strategy history, because looking at what your strategy *would* be is not a change to it. And a
role with no skill weights must be rejected rather than quietly scored, because a role with
nothing to measure produces zero gaps, and zero gaps reads to a student as "you're ready".
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.career_strategy_target import CareerStrategyTarget
from app.models.placement_strategy_snapshot import PlacementStrategySnapshot
from app.models.role import Role
from app.services.career_strategy import service as strategy_service
from app.services.career_strategy import signals, targets
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_interview_evaluation,
    add_job_preferences,
    add_project_build,
    add_resume,
    add_weakness_record,
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


def _student(db):
    """Strong SQL and Python, a weak interview, and a real target role."""
    primary = make_role(db, "Data Engineer", {"SQL": 1.0, "Python": 1.0, "Airflow": 0.8})
    profile = make_profile(db, target_role_id=primary.id, degree="B.Tech", branch="CSE",
                           graduation_year=2026, experience_level="fresher")
    for skill in ("SQL", "Python"):
        add_assessment_result(db, profile, {skill: 0.9}, created_at=days_ago(4))
        add_project_build(db, profile, title=f"{skill} Work", stack_summary=skill)
    add_resume(db, profile, skills=["sql", "python"])
    add_job_preferences(db, profile, normalized_skills=["sql", "python"])
    add_interview_evaluation(db, profile, clarity=0.25, structure=0.3)
    add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=3)
    return profile, primary


class TestSavingTargets:
    def test_targets_are_saved_in_the_order_requested(self, db):
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0, "Excel": 0.6})
        backend = make_role(db, "Backend Engineer", {"Python": 1.0, "APIs": 0.9})

        targets.set_targets(db, profile.id, [backend.id, analyst.id])
        assert [role.name for role in targets.roles_for(db, profile.id)] == [
            "Backend Engineer", "Data Analyst"
        ]

    def test_saving_is_a_replace_not_an_append(self, db):
        """The client never has to diff its own state against the server's."""
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0})
        backend = make_role(db, "Backend Engineer", {"Python": 1.0})

        targets.set_targets(db, profile.id, [analyst.id, backend.id])
        targets.set_targets(db, profile.id, [backend.id])
        assert [role.name for role in targets.roles_for(db, profile.id)] == ["Backend Engineer"]

    def test_an_empty_list_clears_the_comparison(self, db):
        """"Stop comparing" is a real request and has to be expressible."""
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])
        targets.set_targets(db, profile.id, [])
        assert targets.roles_for(db, profile.id) == []

    def test_asking_for_the_same_role_twice_is_one_target(self, db):
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0})
        targets.set_targets(db, profile.id, [analyst.id, analyst.id])
        assert len(targets.roles_for(db, profile.id)) == 1

    def test_an_unknown_role_is_rejected_and_changes_nothing(self, db):
        """Validated before any write: a request naming one bad role must not leave the student
        with half a comparison set they never asked for."""
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])

        with pytest.raises(targets.InvalidTargetError):
            targets.set_targets(db, profile.id, [analyst.id, 987654])
        assert [role.name for role in targets.roles_for(db, profile.id)] == ["Data Analyst"]

    def test_a_role_with_no_skill_weights_is_refused(self, db):
        """A role with nothing to measure against produces zero gaps, and zero gaps reads to a
        student as "you are ready for this". Refusing is the honest answer."""
        profile, _primary = _student(db)
        empty = Role(name="Mystery Role", category="Other", description="No weights")
        db.add(empty)
        db.commit()
        db.refresh(empty)

        with pytest.raises(targets.NoRoleWeightsError):
            targets.set_targets(db, profile.id, [empty.id])
        assert db.query(CareerStrategyTarget).count() == 0

    def test_too_many_targets_is_refused(self, db):
        profile, _primary = _student(db)
        roles = [make_role(db, f"Role {n}", {"SQL": 1.0}) for n in range(targets.MAX_COMPARISON_TARGETS + 1)]
        with pytest.raises(targets.InvalidTargetError):
            targets.set_targets(db, profile.id, [role.id for role in roles])

    def test_targets_are_scoped_per_student(self, db):
        first, _primary = _student(db)
        second = make_profile(db, user_id=2)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0})
        targets.set_targets(db, first.id, [analyst.id])
        assert targets.roles_for(db, second.id) == []


class TestTheEngineIsReused:
    def test_overriding_the_role_changes_what_the_bundle_measures_against(self, db):
        """The entire mechanism, in one assertion: the same collector, a different target."""
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"Excel": 1.0, "Statistics": 0.9})
        context = targets.role_context_for(db, analyst)

        default_bundle = signals.collect(db, profile, now=NOW)
        overridden = signals.collect(db, profile, now=NOW, role=context)

        assert default_bundle.role.display_role_name == "Data Engineer"
        assert overridden.role.display_role_name == "Data Analyst"
        assert "Excel" in overridden.skills and "Excel" not in default_bundle.skills

    def test_a_comparison_run_never_writes_history(self, db):
        """Looking at what your strategy would be for another role is not a change to your
        strategy. compute() forces persist=False when a role is given, so no call site can
        forget and quietly corrupt the timeline."""
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"Excel": 1.0})
        context = targets.role_context_for(db, analyst)

        strategy_service.compute(db, profile, now=NOW)  # the real target: one snapshot
        before = db.query(PlacementStrategySnapshot).count()

        for _ in range(3):
            view = strategy_service.compute(db, profile, now=NOW, role=context, persist=True)
            assert view.snapshot is None
            assert view.snapshot_is_new is False

        assert db.query(PlacementStrategySnapshot).count() == before

    def test_a_comparison_run_is_deterministic(self, db):
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"Excel": 1.0, "SQL": 1.0})
        context = targets.role_context_for(db, analyst)

        first = strategy_service.compute(db, profile, now=NOW, role=context)
        second = strategy_service.compute(db, profile, now=NOW, role=context)
        assert first.decision.model_dump() == second.decision.model_dump()

    def test_a_proven_skill_stays_unrecommended_for_every_target(self, db):
        """The guarantee has to survive the role swap. SQL is proven, so it must not become
        work again merely because a different target also wants it."""
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0, "Excel": 1.0})
        context = targets.role_context_for(db, analyst)

        decision = strategy_service.compute(db, profile, now=NOW, role=context).decision
        assert "SQL" not in {item.subject for item in decision.queue}
        assert "Excel" in {item.subject for item in decision.queue}


class TestComparison:
    def test_the_comparison_covers_the_current_target_and_the_saved_ones(self, db):
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0, "Excel": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])

        comparison = strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        names = [entry.display_role_name for entry in comparison.targets]
        assert names == ["Data Engineer", "Data Analyst"]
        assert comparison.targets[0].is_current_target is True
        assert comparison.targets[1].is_current_target is False

    def test_the_current_target_is_never_listed_twice(self, db):
        """Saving your own target as a comparison target is a reasonable thing for a client to
        do by accident; showing it in two columns is not a reasonable answer."""
        profile, primary = _student(db)
        targets.set_targets(db, profile.id, [primary.id])

        comparison = strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        assert [entry.display_role_name for entry in comparison.targets] == ["Data Engineer"]

    def test_readiness_is_career_decision_labs_number_not_a_second_one(self, db):
        """Two screens disagreeing about one role would make both untrustworthy."""
        from app.services import career_decision_service

        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0, "Excel": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])

        comparison = strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        lab = career_decision_service.simulate(db, profile.id, [analyst.id], None)
        mine = next(e for e in comparison.targets if e.display_role_name == "Data Analyst")
        # Phase 2G gave TargetStrategyRead the same field name as its source. The point of
        # this test is unchanged and now reads directly: there is ONE number, and both screens
        # show it rather than each computing its own.
        assert mine.evidence_coverage_percent == lab.roles[0].evidence_coverage_percent
        assert mine.classification == lab.roles[0].classification

    def test_unique_and_shared_gaps_are_decided_across_the_whole_set(self, db):
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"Airflow": 1.0, "Excel": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])

        comparison = strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        by_name = {entry.display_role_name: entry for entry in comparison.targets}
        # Airflow is wanted by both targets; Excel only by the analyst role.
        assert "Airflow" in by_name["Data Analyst"].shared_gaps
        assert "Airflow" in by_name["Data Engineer"].shared_gaps
        assert "Excel" in by_name["Data Analyst"].unique_gaps

    def test_tradeoffs_never_recommend_a_career(self, db):
        """Which career to pursue is not a decision this system is entitled to make."""
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0, "Excel": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])

        comparison = strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        text = " ".join(comparison.tradeoffs).lower()
        for overreach in ("you should pick", "you should choose", "we recommend", "is the right career",
                          "better career", "choose this role"):
            assert overreach not in text

    def test_no_hiring_probability_in_a_comparison(self, db):
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])

        payload = strategy_service.compare_targets(
            db, profile, is_premium=True, now=NOW
        ).model_dump_json().lower()
        for banned in ("probability", "chance_of", "odds", "likelihood_of_hire", "guaranteed"):
            assert banned not in payload

    def test_effort_estimates_always_carry_their_assumptions(self, db):
        """An hours figure without the assumptions behind it reads as a measurement."""
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0, "Excel": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])

        comparison = strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        assert any(entry.estimated_hours_to_strengthen is not None for entry in comparison.targets)
        assert comparison.assumptions
        assert all(entry.is_estimate for entry in comparison.targets)

    def test_a_free_student_gets_the_shape_and_none_of_the_content(self, db):
        profile, _primary = _student(db)
        analyst = make_role(db, "Data Analyst", {"SQL": 1.0})
        targets.set_targets(db, profile.id, [analyst.id])

        free = strategy_service.compare_targets(db, profile, is_premium=False, now=NOW)
        assert free.locked is True
        assert free.is_premium is False
        assert free.targets == []
        assert free.tradeoffs == []
        assert free.shared_skills == []
        assert free.max_targets == targets.MAX_COMPARISON_TARGETS

    def test_no_targets_and_no_role_is_an_empty_comparison_not_an_error(self, db):
        profile = make_profile(db, user_id=9)
        comparison = strategy_service.compare_targets(db, profile, is_premium=True, now=NOW)
        assert comparison.targets == []
        assert comparison.is_premium is True
