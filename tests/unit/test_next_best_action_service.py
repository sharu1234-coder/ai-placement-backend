import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.career_path_project import CareerPathProject
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.profile import StudentProfile
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.models.skill_gap import SkillGap
from app.services.next_best_action_service import get_next_best_action


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _profile(db) -> StudentProfile:
    profile = StudentProfile(user_id=1)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _career_path(db, profile_id, core_skills, tools=None) -> CareerPath:
    path = CareerPath(
        profile_id=profile_id,
        role_family_key="sde",
        display_role_name="Software Engineer",
        is_primary=True,
        generated_via="catalog",
        role_overview={"core_skills": core_skills, "tools": tools or []},
    )
    db.add(path)
    db.commit()
    db.refresh(path)
    return path


def _target_role(db, profile, skill, weight=1.0) -> Role:
    role = Role(name="Data Analyst", category="Data")
    db.add(role)
    db.commit()
    db.refresh(role)
    db.add(RoleSkillWeight(role_id=role.id, skill=skill, weight=weight, assessment_type="tool"))
    db.commit()
    profile.target_role_id = role.id
    db.commit()
    return role


def _assessment_result(db, profile_id, skill_scores, priority_weaknesses=None, jd_id=None):
    blueprint = AssessmentBlueprint(role_id=None, jd_id=jd_id, profile_id=profile_id, source="x", sections=[])
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
            skill_scores=skill_scores,
            overall_score=0.5,
            priority_weaknesses=priority_weaknesses or [],
        )
    )
    db.commit()


def _jd_skill_gap(db, profile_id, skill_name, severity="high") -> JobDescription:
    jd = JobDescription(profile_id=profile_id, raw_text="text", source="paste")
    db.add(jd)
    db.commit()
    db.refresh(jd)
    match = JobMatch(jd_id=jd.id, profile_id=profile_id, matching_skills=[], missing_skills=[skill_name], match_score=0.0)
    db.add(match)
    db.commit()
    db.refresh(match)
    db.add(SkillGap(job_match_id=match.id, skill_name=skill_name, gap_severity=severity, source="jd_match"))
    db.commit()
    return jd


def _interview_weak_dimension(db, profile_id, dimension="technical_correctness"):
    interview = Interview(profile_id=profile_id, type="technical", mode="text", status="completed")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    scores = {d: 0.9 for d in [
        "technical_correctness", "relevance", "completeness", "reasoning", "structure", "clarity", "role_jd_coverage",
    ]}
    scores[dimension] = 0.3
    db.add(
        InterviewEvaluation(
            interview_id=interview.id,
            technical_correctness=scores["technical_correctness"],
            relevance=scores["relevance"],
            completeness=scores["completeness"],
            reasoning=scores["reasoning"],
            structure=scores["structure"],
            clarity=scores["clarity"],
            role_jd_coverage=scores["role_jd_coverage"],
            overall_score=0.7,
            feedback_text="ok",
        )
    )
    db.commit()


def test_no_role_returns_no_recommendation(db):
    profile = _profile(db)
    result = get_next_best_action(db, profile)
    assert result.has_recommendation is False
    assert result.tier is None


def test_empty_role_skills_returns_no_recommendation(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=[], tools=[])
    result = get_next_best_action(db, profile)
    assert result.has_recommendation is False


def test_tier1_critical_role_gap_for_unclaimed_core_skill(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    result = get_next_best_action(db, profile)
    assert result.tier == "critical_role_gap"
    assert result.skill == "Python"
    assert result.action_type == "learn"
    assert "Python" in result.title


def test_tier1_picks_alphabetically_first_among_ties(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Zebra", "Aardvark"])
    result = get_next_best_action(db, profile)
    assert result.tier == "critical_role_gap"
    assert result.skill == "Aardvark"


def test_tier1_does_not_fire_for_tools_only(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=[], tools=["Docker"])
    result = get_next_best_action(db, profile)
    assert result.tier != "critical_role_gap"


def test_tier2_repeated_weakness_beats_single_source_tiers(db):
    profile = _profile(db)
    _target_role(db, profile, skill="SQL", weight=1.0)
    # Real evidence for SQL so tier1 doesn't fire (state won't be "claimed").
    _assessment_result(db, profile.id, {"SQL": 0.9})
    # A second assessment result whose priority_weaknesses flags SQL (role-scoped, jd_id=None).
    _assessment_result(db, profile.id, {"SQL": 0.2}, priority_weaknesses=["SQL"])
    # A JD gap on the same skill -- merged, SQL now has 2 independent sources.
    _jd_skill_gap(db, profile.id, "SQL")

    result = get_next_best_action(db, profile)
    assert result.tier == "repeated_weakness"
    assert result.skill == "SQL"


def test_tier3_assessment_only_weakness(db):
    profile = _profile(db)
    _target_role(db, profile, skill="SQL", weight=1.0)
    _assessment_result(db, profile.id, {"SQL": 0.9})  # real evidence, avoids tier1
    _assessment_result(db, profile.id, {"SQL": 0.2}, priority_weaknesses=["SQL"])

    result = get_next_best_action(db, profile)
    assert result.tier == "assessment_weakness"
    assert result.skill == "SQL"


def test_tier4_interview_only_weakness(db):
    profile = _profile(db)
    _target_role(db, profile, skill="SQL", weight=1.0)
    _assessment_result(db, profile.id, {"SQL": 0.9})  # real evidence, avoids tier1
    _interview_weak_dimension(db, profile.id, dimension="technical_correctness")

    result = get_next_best_action(db, profile)
    assert result.tier == "interview_weakness"
    assert result.skill == "interview:technical_correctness"
    assert "technical correctness" in result.title.lower()


def test_tier5_project_requirement(db):
    profile = _profile(db)
    path = _career_path(db, profile.id, core_skills=["SQL"])
    # Real evidence avoids tier1 -- an empty weights dict would short-circuit before tier5 ever runs.
    _assessment_result(db, profile.id, {"SQL": 0.9})
    db.add(
        CareerPathProject(
            career_path_id=path.id,
            project_key="p1",
            title="ETL Pipeline",
            skills_demonstrated=[],
            technologies=[],
            order_index=0,
            status="not_started",
        )
    )
    db.commit()

    result = get_next_best_action(db, profile)
    assert result.tier == "project_requirement"
    assert "ETL Pipeline" in result.title


def test_tier6_resume_jd_gap(db):
    profile = _profile(db)
    _target_role(db, profile, skill="SQL", weight=1.0)
    _assessment_result(db, profile.id, {"SQL": 0.9})  # real evidence, avoids tier1
    _jd_skill_gap(db, profile.id, "SQL")

    result = get_next_best_action(db, profile)
    assert result.tier == "resume_jd_gap"
    assert result.skill == "SQL"


def test_tier7_roadmap_dependency(db):
    profile = _profile(db)
    _target_role(db, profile, skill="SQL", weight=1.0)
    _assessment_result(db, profile.id, {"SQL": 0.9})  # real evidence, avoids tier1
    placement_path = PlacementPath(profile_id=profile.id, status="active")
    db.add(placement_path)
    db.commit()
    db.refresh(placement_path)
    db.add(
        PlacementPathTopic(
            path_id=placement_path.id,
            stage="foundations",
            track_key="sql",
            topic_key="sql_joins",
            order_in_track=0,
            title="SQL Joins",
            skill_tag="SQL",
            status="not_started",
        )
    )
    db.commit()

    result = get_next_best_action(db, profile)
    assert result.tier == "roadmap_dependency"
    assert "SQL Joins" in result.title


def test_tier8_stale_skill(db):
    import datetime

    profile = _profile(db)
    _target_role(db, profile, skill="SQL", weight=1.0)
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=200)
    _assessment_result(db, profile.id, {"SQL": 0.9})
    # Overwrite created_at on that result to be old, forcing "stale" instead of "supported".
    from app.models.assessment_result import AssessmentResult as AR

    result_row = db.query(AR).first()
    result_row.created_at = old
    db.commit()

    result = get_next_best_action(db, profile)
    assert result.tier == "stale_skill"
    assert result.skill == "SQL"


def test_tier9_maintenance_fallback_when_nothing_else_fires(db):
    profile = _profile(db)
    _target_role(db, profile, skill="SQL", weight=1.0)
    _assessment_result(db, profile.id, {"SQL": 0.9})  # supported, not claimed/stale, no weaknesses

    result = get_next_best_action(db, profile)
    assert result.tier == "maintenance"
    assert result.has_recommendation is True
    assert result.skill == "SQL"


def test_priority_order_tier1_beats_tier3_when_both_apply(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python", "SQL"])
    # SQL gets a real evidence-backed weakness (tier3 material)...
    _assessment_result(db, profile.id, {"SQL": 0.2}, priority_weaknesses=["SQL"])
    # ...but Python has zero evidence at all -- tier1 must win regardless.
    result = get_next_best_action(db, profile)
    assert result.tier == "critical_role_gap"
    assert result.skill == "Python"


def test_reasons_are_always_populated(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    result = get_next_best_action(db, profile)
    assert len(result.reasons) > 0
    assert all(isinstance(r, str) and r for r in result.reasons)


def test_estimated_minutes_is_present_for_every_action_type(db):
    profile = _profile(db)
    _career_path(db, profile.id, core_skills=["Python"])
    result = get_next_best_action(db, profile)
    assert result.estimated_minutes is not None
    assert result.estimated_minutes > 0
