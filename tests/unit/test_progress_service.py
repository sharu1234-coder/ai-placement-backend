import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.roadmap import Roadmap
from app.models.roadmap_task import RoadmapTask
from app.models.role import Role
from app.services.progress_service import get_dashboard


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _at(hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 8, 23, hour, tzinfo=datetime.timezone.utc)


def test_no_data_returns_empty_dashboard(db):
    dashboard = get_dashboard(db, profile_id=1)
    assert dashboard.performance_score is None
    assert dashboard.completed_assessments == 0
    assert dashboard.completed_interviews == 0
    assert dashboard.roadmap_tasks_done == 0
    assert dashboard.roadmap_tasks_total == 0
    assert dashboard.top_weaknesses == []
    assert dashboard.trend == []


def _make_assessment_result(db, profile_id, overall_score, created_at, role_name="SDE"):
    role = db.query(Role).filter(Role.name == role_name).first()
    if role is None:
        role = Role(name=role_name)
        db.add(role)
        db.commit()
        db.refresh(role)
    blueprint = AssessmentBlueprint(role_id=role.id, jd_id=None, profile_id=profile_id, source="default_taxonomy", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"DSA": overall_score},
            overall_score=overall_score,
            priority_weaknesses=[] if overall_score >= 0.6 else ["DSA"],
            created_at=created_at,
        )
    )
    db.commit()


def _make_interview_evaluation(db, profile_id, overall_score, created_at, interview_type="technical", mode="text"):
    interview = Interview(profile_id=profile_id, type=interview_type, mode=mode, jd_id=None, status="completed")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    db.add(
        InterviewEvaluation(
            interview_id=interview.id,
            technical_correctness=overall_score,
            relevance=overall_score,
            completeness=overall_score,
            reasoning=overall_score,
            structure=overall_score,
            clarity=overall_score,
            grammar=overall_score,
            speaking_pace=None,
            filler_word_rate=None,
            role_jd_coverage=overall_score,
            overall_score=overall_score,
            feedback_text="ok",
            created_at=created_at,
        )
    )
    db.commit()


def test_performance_score_averages_latest_assessment_and_interview(db):
    _make_assessment_result(db, profile_id=1, overall_score=0.4, created_at=_at(9))
    _make_interview_evaluation(db, profile_id=1, overall_score=0.8, created_at=_at(10))

    dashboard = get_dashboard(db, profile_id=1)
    assert dashboard.performance_score == pytest.approx(0.6)
    assert dashboard.completed_assessments == 1
    assert dashboard.completed_interviews == 1


def test_performance_score_uses_only_available_signal(db):
    _make_assessment_result(db, profile_id=1, overall_score=0.5, created_at=_at(9))

    dashboard = get_dashboard(db, profile_id=1)
    assert dashboard.performance_score == pytest.approx(0.5)


def test_performance_score_uses_most_recent_of_each_kind(db):
    _make_assessment_result(db, profile_id=1, overall_score=0.2, created_at=_at(8))
    _make_assessment_result(db, profile_id=1, overall_score=0.9, created_at=_at(11))

    dashboard = get_dashboard(db, profile_id=1)
    assert dashboard.performance_score == pytest.approx(0.9)


def test_trend_sorted_chronologically_across_both_kinds(db):
    _make_interview_evaluation(db, profile_id=1, overall_score=0.7, created_at=_at(12))
    _make_assessment_result(db, profile_id=1, overall_score=0.3, created_at=_at(8))

    dashboard = get_dashboard(db, profile_id=1)
    assert [point.kind for point in dashboard.trend] == ["assessment", "interview"]
    assert dashboard.trend[0].score == 0.3
    assert dashboard.trend[1].score == 0.7
    assert "SDE" in dashboard.trend[0].label


def test_roadmap_task_counts_only_active_roadmap(db):
    active = Roadmap(profile_id=1, jd_id=None, duration_days=7, status="active")
    superseded = Roadmap(profile_id=1, jd_id=None, duration_days=7, status="superseded")
    db.add_all([active, superseded])
    db.commit()
    db.refresh(active)
    db.refresh(superseded)

    db.add_all(
        [
            RoadmapTask(
                roadmap_id=active.id, day_number=1, task_type="learn", skill_tag="DSA", title="t", description="d",
                status="done", due_date=datetime.date(2026, 8, 23),
            ),
            RoadmapTask(
                roadmap_id=active.id, day_number=1, task_type="practice", skill_tag="DSA", title="t", description="d",
                status="pending", due_date=datetime.date(2026, 8, 23),
            ),
            RoadmapTask(
                roadmap_id=superseded.id, day_number=1, task_type="learn", skill_tag="DSA", title="t", description="d",
                status="done", due_date=datetime.date(2026, 8, 20),
            ),
        ]
    )
    db.commit()

    dashboard = get_dashboard(db, profile_id=1)
    assert dashboard.roadmap_tasks_done == 1
    assert dashboard.roadmap_tasks_total == 2


def test_get_dashboard_query_count_does_not_scale_with_assessment_count(db):
    """Locks in the wider-audit N+1 fix: _assessment_label's JD/Role lookups are batched
    once per get_dashboard call (jd_by_id/role_by_id, a single .in_() query each) instead of
    once per assessment row (the old db.get() per row) -- so 5 assessments against 5
    distinct roles must issue the exact same query count as 1, never 5x."""
    _make_assessment_result(db, profile_id=1, overall_score=0.5, created_at=_at(9), role_name="Role0")

    engine = db.get_bind()
    queries = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        get_dashboard(db, profile_id=1)
        one_assessment_query_count = len(queries)

        for i in range(1, 5):
            _make_assessment_result(db, profile_id=1, overall_score=0.5, created_at=_at(9 + i), role_name=f"Role{i}")

        queries.clear()
        get_dashboard(db, profile_id=1)
        five_assessment_query_count = len(queries)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert one_assessment_query_count > 0
    assert five_assessment_query_count == one_assessment_query_count


def test_top_weaknesses_limited_to_five(db):
    role = Role(name="SDE")
    db.add(role)
    db.commit()
    db.refresh(role)
    blueprint = AssessmentBlueprint(role_id=role.id, jd_id=None, profile_id=1, source="default_taxonomy", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=1, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    skills = ["A", "B", "C", "D", "E", "F"]
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={s: 0.1 for s in skills},
            overall_score=0.1,
            priority_weaknesses=skills,
            created_at=_at(9),
        )
    )
    db.commit()

    dashboard = get_dashboard(db, profile_id=1)
    assert len(dashboard.top_weaknesses) == 5
