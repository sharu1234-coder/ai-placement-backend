"""The grounding layer's query count must not grow with the size of the job description.

The grounded sections read more than the old plan did -- resume, analysis, projects,
requirements -- and every one of those is a place where a per-requirement or per-project query
could creep in unnoticed. A JD with forty requirements would then cost forty extra round trips,
invisible on SQLite in a unit test and very visible on a real database.

Scope, established by measurement rather than assumption. Counting the whole of
generate_preparation_plan shows growth (28 statements for 3 requirements, 102 for 40), and
attributing those statements to their call sites puts every one of the extra queries in
matching_service.py's per-gap refresh loop -- code this feature did not touch, and which is
deliberately left authoritative. SQLAlchemy expires attributes on commit, so building one
SkillGapRead per gap re-selects each row; that is inherent to how that function returns its
result, and unpicking it is a change to the matching engine, not to this feature.

What this test therefore pins is the part this feature owns: the grounding layer stays flat no
matter how large the JD or how many projects the student has. The pre-existing growth is
recorded above so that a future reader knows it was measured, not missed.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.provider import AIProvider
from app.ai.schemas.job_preparation_plan import GeneratedJobPreparationContent
from app.database.base import Base
from app.models.job_description import JobDescription
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.requirement import Requirement
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.user import User
from app.services import jd_grounding


class _SilentProvider(AIProvider):
    """Returns nothing usable on purpose -- this test is about queries, not content, and the
    deterministic half of the plan is produced either way."""

    def generate_structured(self, prompt, schema):
        return GeneratedJobPreparationContent(category_reasons=[], interview_questions=[])

    def generate_text(self, prompt):
        return ""


@pytest.fixture()
def engine_and_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield engine, session
    session.close()


def _seed(db, *, requirement_count: int, project_count: int, email: str) -> tuple:
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    profile = StudentProfile(user_id=user.id, full_name="Asha Rao")
    db.add(profile)
    db.commit()
    db.refresh(profile)

    resume = Resume(
        profile_id=profile.id,
        file_path="resumes/x.pdf",
        original_filename="cv.pdf",
        file_size_bytes=10,
        raw_text="text",
        parse_status="completed",
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    db.add(
        ResumeAnalysis(
            resume_id=resume.id,
            extracted_skills=["SQL"],
            extracted_education=[],
            extracted_experience=[],
            extracted_projects=[{"title": f"Project {i}", "tech_stack": "SQL"} for i in range(project_count)],
            extracted_certifications=[],
            ats_score=70.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    for i in range(project_count):
        db.add(Project(profile_id=profile.id, title=f"Profile Project {i}", tech_stack="SQL"))

    jd = JobDescription(profile_id=profile.id, raw_text="jd", source="paste", role_title="Data Analyst")
    db.add(jd)
    db.commit()
    db.refresh(jd)
    db.add_all(
        Requirement(jd_id=jd.id, category="skill", value=f"Skill{i}", is_explicit=True, importance="required")
        for i in range(requirement_count)
    )
    db.commit()
    return profile.id, jd.id


def _count_queries(engine, fn) -> int:
    counter = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    return counter["n"]


def test_grounding_query_count_does_not_grow_with_the_size_of_the_job_description(engine_and_session):
    engine, db = engine_and_session

    def ground(profile_id, jd_id):
        def run():
            facts = jd_grounding.collect_resume_facts(db, profile_id)
            requirements = jd_grounding.matchable_requirements(db, jd_id)
            jd_grounding.missing_keywords(requirements, facts)
            jd_grounding.match_projects(requirements, facts)
            jd_grounding.ground_gaps(requirements, {r.value: "high" for r in requirements})

        return run

    small_profile, small_jd = _seed(db, requirement_count=3, project_count=2, email="small@example.com")
    small = _count_queries(engine, ground(small_profile, small_jd))

    big_profile, big_jd = _seed(db, requirement_count=40, project_count=25, email="big@example.com")
    big = _count_queries(engine, ground(big_profile, big_jd))

    assert big == small, (
        f"grounding query count grew with input size ({small} -> {big}): something now queries "
        "per requirement or per project"
    )
