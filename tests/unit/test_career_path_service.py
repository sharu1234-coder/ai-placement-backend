import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.career_path import GeneratedRoleCatalog, GeneratedRoleOverview, GeneratedRolePhase, GeneratedRoleSkill
from app.database.base import Base
from app.models.project import Project
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.role import Role
from app.services.career_path import service as svc

PROFILE_ID = 1
OTHER_PROFILE_ID = 2


class FailingAIProvider(AIProvider):
    """Every career_path_service test exercises the AI-failure fallback path deliberately --
    generation must never depend on AI succeeding, per the "never blocks" rule."""

    def generate_structured(self, prompt, schema):
        raise AIGenerationError("no AI in unit tests")

    def generate_text(self, prompt):
        return "fake"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def ai():
    return FailingAIProvider()


@pytest.fixture()
def data_analyst_role(db):
    role = Role(name="Data Analyst", category="Data")
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


def _insert_resume(db, profile_id, skills):
    resume = Resume(profile_id=profile_id, file_path="x.pdf", raw_text="text", parse_status="completed")
    db.add(resume)
    db.commit()
    db.refresh(resume)
    db.add(
        ResumeAnalysis(
            resume_id=resume.id,
            extracted_skills=skills,
            extracted_education=[],
            extracted_experience=[],
            extracted_projects=[],
            extracted_certifications=[],
            ats_score=70.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def test_select_role_with_no_resume_never_raises_and_produces_content(db, ai, data_analyst_role):
    read = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    assert read.role_family_key == "data_analyst"
    assert read.generated_via == "catalog"
    assert read.is_primary is True
    assert len(read.phases) == 5
    assert sum(len(p.skill_items) for p in read.phases) > 0
    assert sum(len(p.projects) for p in read.phases) == 3
    # No resume yet -- must be honestly absent, not fabricated.
    assert read.resume_connection is None
    assert read.job_readiness.overall_percent == 0
    assert all(g.status in ("missing", "recommended") for g in read.skill_gaps)


def test_select_role_custom_uncurated_role_falls_back_gracefully(db, ai):
    read = svc.select_role(db, PROFILE_ID, ai, custom_role="Petroleum Engineer")
    assert read.role_family_key == "custom:petroleum_engineer"
    assert read.generated_via == "fallback"
    assert sum(len(p.skill_items) for p in read.phases) > 0
    assert read.job_readiness.is_estimate is True


def test_select_role_invalid_role_id_raises(db, ai):
    with pytest.raises(svc.RoleNotFoundError):
        svc.select_role(db, PROFILE_ID, ai, role_id=999999)


def test_select_role_twice_reuses_same_row(db, ai, data_analyst_role):
    first = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    second = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    assert first.id == second.id


def test_selecting_a_new_role_does_not_delete_the_previous_one(db, ai, data_analyst_role):
    first = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    svc.select_role(db, PROFILE_ID, ai, custom_role="Petroleum Engineer")

    first_again = svc.get_path(db, PROFILE_ID, first.id)
    assert first_again.is_primary is False  # no longer current...
    summaries = svc.list_all(db, PROFILE_ID)
    assert len(summaries) == 2  # ...but never deleted


def test_refresh_carries_forward_completed_status(db, ai, data_analyst_role):
    read = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    item = read.phases[0].skill_items[0]
    svc.mark_skill_item_status(db, PROFILE_ID, item.id, "completed")

    refreshed = svc.refresh(db, PROFILE_ID, read.id, ai)
    same_item = next(i for p in refreshed.phases for i in p.skill_items if i.skill_key == item.skill_key)
    assert same_item.status == "completed"
    assert refreshed.is_primary is True  # refresh never changes primary


class VaryingAIProvider(AIProvider):
    """Simulates a real AI's non-determinism: each generate_structured call for a role
    definition returns a *different* skill_key, the way a real LLM is not guaranteed to repeat
    the same slug/title across two separate calls for the same role name. Used to prove
    select_role() caches and reuses an AI-assisted role's generated definition rather than
    re-rolling the AI on every re-selection -- without that cache, re-selecting the same custom
    role would silently reset the student's completed-skill status (regression test for the
    carry-forward-by-key bug in career_path/service.py's role resolution)."""

    def __init__(self):
        self.role_calls = 0

    def generate_structured(self, prompt, schema):
        if schema is GeneratedRoleCatalog:
            self.role_calls += 1
            return GeneratedRoleCatalog(
                overview=GeneratedRoleOverview(
                    short_explanation="Builds things.",
                    responsibilities=["Do work"],
                    core_skills=["Skill A"],
                    tools=["Tool A"],
                    difficulty="moderate",
                    entry_level_expectations="Some baseline.",
                    interview_focus=["Fundamentals"],
                    portfolio_expectations="One project.",
                ),
                phases=[
                    GeneratedRolePhase(
                        phase_key="foundations",
                        skills=[GeneratedRoleSkill(skill_key=f"skill_call_{self.role_calls}", title="Skill", level="beginner")],
                    ),
                ],
            )
        raise AIGenerationError("explanations not exercised by this fake")

    def generate_text(self, prompt):
        return "fake"


def test_reselecting_custom_role_reuses_cached_definition_and_carries_forward_status(db):
    ai = VaryingAIProvider()
    first = svc.select_role(db, PROFILE_ID, ai, custom_role="Blockchain Developer")
    assert first.generated_via == "ai_generated"
    assert ai.role_calls == 1
    item = first.phases[0].skill_items[0]
    assert item.skill_key == "skill_call_1"
    svc.mark_skill_item_status(db, PROFILE_ID, item.id, "completed")

    second = svc.select_role(db, PROFILE_ID, ai, custom_role="Blockchain Developer")
    # Re-selecting the same custom role must NOT re-call the AI...
    assert ai.role_calls == 1
    # ...and must reuse the exact same skill_key, so completion status actually carries forward.
    same_item = next(i for p in second.phases for i in p.skill_items if i.skill_key == "skill_call_1")
    assert same_item.status == "completed"


def test_refresh_of_custom_role_regenerates_via_ai_and_updates_the_cache(db):
    ai = VaryingAIProvider()
    first = svc.select_role(db, PROFILE_ID, ai, custom_role="Blockchain Developer")
    assert ai.role_calls == 1

    refreshed = svc.refresh(db, PROFILE_ID, first.id, ai)
    # refresh() is the one explicit action expected to call the AI again.
    assert ai.role_calls == 2
    assert refreshed.phases[0].skill_items[0].skill_key == "skill_call_2"

    # And the newly refreshed definition is what a subsequent re-selection now reuses.
    reselected = svc.select_role(db, PROFILE_ID, ai, custom_role="Blockchain Developer")
    assert ai.role_calls == 2
    assert reselected.phases[0].skill_items[0].skill_key == "skill_call_2"


# ---------------------------------------------------------------------------
# Gap analysis / resume connection -- honesty checks
# ---------------------------------------------------------------------------


def test_gap_analysis_reflects_real_resume_skills(db, ai, data_analyst_role):
    _insert_resume(db, PROFILE_ID, ["sql", "excel", "python"])
    read = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)

    strong = {g.skill.lower() for g in read.skill_gaps if g.status == "strong"}
    assert "sql" in strong
    assert "excel" in strong
    # A skill the resume never mentions must never show as strong.
    missing = {g.skill.lower() for g in read.skill_gaps if g.status == "missing"}
    assert "power bi or tableau" in missing or "power bi" in " ".join(missing)


def test_resume_connection_counts_are_never_fabricated(db, ai, data_analyst_role):
    _insert_resume(db, PROFILE_ID, ["sql"])
    db.add(Project(profile_id=PROFILE_ID, title="SQL Reporting Tool", description="Built with SQL", tech_stack="SQL"))
    db.commit()

    read = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    conn = read.resume_connection
    assert conn is not None
    assert conn.skills_demonstrated >= 1
    assert conn.skills_demonstrated <= conn.skills_total
    assert conn.projects_demonstrated <= conn.projects_total
    assert 0 <= conn.role_keyword_coverage_percent <= 100


# ---------------------------------------------------------------------------
# Ownership isolation
# ---------------------------------------------------------------------------


def test_cross_profile_access_is_blocked_not_found(db, ai, data_analyst_role):
    read = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)

    with pytest.raises(svc.CareerPathNotFoundError):
        svc.get_path(db, OTHER_PROFILE_ID, read.id)

    item_id = read.phases[0].skill_items[0].id
    with pytest.raises(svc.SkillItemNotFoundError):
        svc.mark_skill_item_status(db, OTHER_PROFILE_ID, item_id, "completed")

    project_id = next(p.projects[0].id for p in read.phases if p.projects)
    with pytest.raises(svc.ProjectNotFoundError):
        svc.mark_project_status(db, OTHER_PROFILE_ID, project_id, "completed")

    with pytest.raises(svc.CareerPathNotFoundError):
        svc.set_current(db, OTHER_PROFILE_ID, read.id)


# ---------------------------------------------------------------------------
# Practice handoff / project completion
# ---------------------------------------------------------------------------


def test_practice_blueprint_is_idempotent(db, ai, data_analyst_role):
    read = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    item_id = read.phases[0].skill_items[0].id
    bp1 = svc.get_or_create_practice_blueprint(db, PROFILE_ID, item_id)
    bp2 = svc.get_or_create_practice_blueprint(db, PROFILE_ID, item_id)
    assert bp1 == bp2


def test_marking_all_phase_children_complete_marks_phase_completed(db, ai, data_analyst_role):
    read = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    contact_phase = next(p for p in read.phases if p.phase_key == "foundations")
    for item in contact_phase.skill_items:
        svc.mark_skill_item_status(db, PROFILE_ID, item.id, "completed")

    updated = svc.get_path(db, PROFILE_ID, read.id)
    updated_phase = next(p for p in updated.phases if p.phase_key == "foundations")
    assert updated_phase.status == "completed"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_generating_same_curated_role_produces_the_same_structure(db, ai, data_analyst_role):
    read = svc.select_role(db, PROFILE_ID, ai, role_id=data_analyst_role.id)
    keys1 = [(i.skill_key, p.phase_key) for p in read.phases for i in p.skill_items]

    other_profile_role = svc.select_role(db, OTHER_PROFILE_ID, ai, role_id=data_analyst_role.id)
    keys2 = [(i.skill_key, p.phase_key) for p in other_profile_role.phases for i in p.skill_items]

    assert keys1 == keys2


# ---------------------------------------------------------------------------
# Job readiness -- averaging only measured signals
# ---------------------------------------------------------------------------


def test_job_readiness_does_not_dilute_real_scores_with_absent_signals():
    """The reported harm: a student genuinely scoring 80% on practice, with no resume uploaded
    and no interviews taken, used to be told they were "16% job ready" -- because the average
    divided by a constant 5 and counted the two things they simply hadn't done yet as measured
    zeros. Absence of data must not read as a verdict on the student.
    """
    readiness = svc._compute_job_readiness(
        "Data Analyst",
        skills_percent=0,
        projects_percent=0,
        resume_percent=None,
        practice_percent=80,
        interview_percent=None,
        is_estimate=False,
    )

    assert readiness.overall_percent == 27  # (0 + 0 + 80) / 3, not (0+0+0+80+0) / 5 == 16
    assert readiness.contributing_signals == 3
    assert readiness.total_signals == 5
    # The untaken signals stay honestly absent rather than being reported as zero.
    assert readiness.resume_percent is None
    assert readiness.interview_percent is None


def test_job_readiness_with_all_five_signals_is_a_plain_average():
    """Regression guard on the other side of the fix: once every signal has real data, the
    number must be exactly what it always was."""
    readiness = svc._compute_job_readiness(
        "Data Analyst",
        skills_percent=50,
        projects_percent=60,
        resume_percent=70,
        practice_percent=80,
        interview_percent=90,
        is_estimate=False,
    )

    assert readiness.overall_percent == 70  # (50+60+70+80+90) / 5
    assert readiness.contributing_signals == 5


def test_job_readiness_focus_areas_never_name_an_unmeasured_signal():
    """Telling a student to improve a signal we have no reading for would be advice derived
    from an absence rather than from evidence."""
    readiness = svc._compute_job_readiness(
        "Data Analyst",
        skills_percent=90,
        projects_percent=95,
        resume_percent=None,
        practice_percent=None,
        interview_percent=None,
        is_estimate=False,
    )

    assert readiness.contributing_signals == 2
    assert len(readiness.top_focus_areas) == 2  # only the two that were actually measured
    joined = " ".join(readiness.top_focus_areas).lower()
    assert "resume" not in joined
    assert "interview" not in joined
