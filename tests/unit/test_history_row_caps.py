"""Disclosed gap, addressed as a defensive (not full pagination) fix: 5 history/list
endpoints (assessment history, interview list, placement path history, resume ATS-check
list, career path list) returned every row a profile had ever accumulated with no limit.
True cursor/page-based pagination would need a coordinated backend+Android API contract
change (the response shape would have to become an envelope, not a bare list) -- out of
scope for a same-shape defensive fix under this pass. Instead, each query now caps at a
generous row count (200) via `.limit()`, ordered most-recent-first as they already were, so
a long-lived account can never make one of these responses grow unbounded. This is a safety
net against a real (if slow-developing) resource-growth risk, not a UX pagination feature --
still disclosed as a remaining product gap in the release report.

These tests insert rows directly (bypassing the AI/lifecycle plumbing each service's other
tests already cover) purely to prove the row cap is actually applied at the query level."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.interview import Interview
from app.models.job_description import JobDescription
from app.models.placement_path import PlacementPath
from app.models.profile import StudentProfile  # noqa: F401  (registers the FK target table)
from app.models.project_build import ProjectBuild
from app.models.recruiter_attack_session import RecruiterAttackSession
from app.models.resume import Resume
from app.models.resume_ats_check import ResumeAtsCheck
from app.services import (
    assessment_service,
    interview_service,
    jd_service,
    placement_path_service,
    recruiter_attack_service,
)
from app.services.ats_check_service import list_ats_checks
from app.services.career_path import service as career_path_service
from app.services.project_builder import service as project_builder_service

PROFILE_ID = 1
_OVER_CAP = 205  # comfortably past every service's 200-row cap


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_assessment_history_is_capped(db):
    for i in range(_OVER_CAP):
        blueprint = AssessmentBlueprint(profile_id=PROFILE_ID, source="role", sections=[])
        db.add(blueprint)
        db.flush()
        assessment = Assessment(profile_id=PROFILE_ID, blueprint_id=blueprint.id, status="completed")
        db.add(assessment)
        db.flush()
        db.add(
            AssessmentResult(
                assessment_id=assessment.id,
                overall_score=1.0,
                skill_scores={},
                priority_weaknesses=[],
                created_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=i),
            )
        )
    db.commit()

    history = assessment_service.get_assessment_history(db, PROFILE_ID)
    assert len(history) == assessment_service._MAX_HISTORY_ROWS


def test_interview_list_is_capped(db):
    for i in range(_OVER_CAP):
        db.add(
            Interview(
                profile_id=PROFILE_ID,
                type="technical",
                mode="text",
                status="completed",
                created_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=i),
            )
        )
    db.commit()

    history = interview_service.list_interviews(db, PROFILE_ID)
    assert len(history) == interview_service._MAX_HISTORY_ROWS


def test_placement_path_history_is_capped(db):
    for i in range(_OVER_CAP):
        db.add(
            PlacementPath(
                profile_id=PROFILE_ID,
                status="superseded",
                created_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=i),
            )
        )
    db.commit()

    history = placement_path_service.get_placement_path_history(db, PROFILE_ID)
    assert len(history) == placement_path_service._MAX_HISTORY_ROWS


def test_ats_check_list_is_capped(db):
    resume = Resume(
        profile_id=PROFILE_ID,
        file_path="resumes/resume.pdf",
        original_filename="resume.pdf",
        raw_text="Sample resume text.",
        parse_status="completed",
    )
    db.add(resume)
    db.flush()
    for _ in range(_OVER_CAP):
        db.add(
            ResumeAtsCheck(
                profile_id=PROFILE_ID,
                resume_id=resume.id,
                overall_score=80,
                score_category="good",
                parsing_score=80,
                structure_score=80,
                content_score=80,
                skills_score=80,
                experience_score=80,
                projects_score=80,
                contact_score=80,
                readability_score=80,
                category_breakdown=[],
                section_analysis=[],
                issues=[],
                recommendations=[],
                ai_summary="Looks good.",
            )
        )
    db.commit()

    from app.services import ats_check_service

    history = list_ats_checks(db, PROFILE_ID)
    assert len(history) == ats_check_service._MAX_HISTORY_ROWS


def test_recruiter_attack_list_sessions_is_capped(db):
    """Real audit gap, now fixed: list_sessions previously had no equivalent to the
    _MAX_HISTORY_ROWS cap every sibling list endpoint already applies."""
    for i in range(_OVER_CAP):
        db.add(
            RecruiterAttackSession(
                profile_id=PROFILE_ID,
                source_type="resume_claim",
                source_label=f"Claim {i}",
                status="completed",
                created_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=i),
            )
        )
    db.commit()

    sessions = recruiter_attack_service.list_sessions(db, PROFILE_ID)
    assert len(sessions) == recruiter_attack_service._MAX_HISTORY_ROWS


def test_project_builder_list_builds_is_capped(db):
    for i in range(_OVER_CAP):
        db.add(
            ProjectBuild(
                profile_id=PROFILE_ID,
                title=f"Build {i}",
                status="not_started",
                created_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=i),
            )
        )
    db.commit()

    builds = project_builder_service.list_builds(db, PROFILE_ID)
    assert len(builds) == project_builder_service._MAX_HISTORY_ROWS


def test_jd_service_list_job_descriptions_is_capped(db):
    for i in range(_OVER_CAP):
        db.add(
            JobDescription(
                profile_id=PROFILE_ID,
                raw_text=f"JD text {i}",
                created_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=i),
            )
        )
    db.commit()

    jds = jd_service.list_job_descriptions(db, PROFILE_ID)
    assert len(jds) == jd_service._MAX_HISTORY_ROWS


def test_career_path_list_all_is_capped(db):
    for i in range(_OVER_CAP):
        db.add(
            CareerPath(
                profile_id=PROFILE_ID,
                custom_role=f"Role {i}",
                role_family_key=f"custom:role-{i}",
                display_role_name=f"Role {i}",
                role_overview={},
                is_primary=False,
                generated_via="fallback",
                updated_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=i),
            )
        )
    db.commit()

    paths = career_path_service.list_all(db, PROFILE_ID)
    assert len(paths) == career_path_service._MAX_SAVED_PATHS
