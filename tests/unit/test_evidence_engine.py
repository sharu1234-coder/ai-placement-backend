import datetime

import pytest
from sqlalchemy import create_engine, event
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
from app.models.interview_question import InterviewQuestion
from app.models.project import Project
from app.models.project_build import ProjectBuild
from app.models.question import Question
from app.models.question_attempt import QuestionAttempt
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.services.evidence_engine import get_all_skill_evidence, get_skill_evidence

_UTC = datetime.timezone.utc


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _blueprint_and_assessment(db, profile_id=1):
    blueprint = AssessmentBlueprint(role_id=None, jd_id=None, profile_id=profile_id, source="default_taxonomy", sections=[])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return assessment


def test_no_data_is_claimed_with_zero_confidence_and_no_last_verified(db):
    view = get_skill_evidence(db, profile_id=1, skill="Python")
    assert view.state == "claimed"
    assert view.confidence == 0.0
    assert view.items == []
    assert view.last_verified_at is None


def test_resume_only_is_supported(db):
    resume = Resume(profile_id=1, file_path="x", raw_text="...", parse_status="completed")
    db.add(resume)
    db.commit()
    db.refresh(resume)
    db.add(
        ResumeAnalysis(
            resume_id=resume.id,
            extracted_skills=["Python"],
            extracted_education=[],
            extracted_experience=[],
            extracted_projects=[],
            extracted_certifications=[],
            ats_score=80.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Python")
    assert view.state == "supported"
    assert view.confidence == 0.45
    assert len(view.items) == 1
    assert view.items[0].source == "resume"
    assert view.items[0].label == "Resume evidence detected"


def test_assessment_evidence_matches_by_token_and_scores_out_of_100(db):
    assessment = _blueprint_and_assessment(db)
    now = datetime.datetime.now(_UTC)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"SQL": 0.91},
            overall_score=0.91,
            priority_weaknesses=[],
            created_at=now,
        )
    )
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="SQL")
    assert view.state == "supported"
    assert len(view.items) == 1
    item = view.items[0]
    assert item.source == "assessment"
    assert item.score == 91.0
    assert item.label == "Assessment — 91%"
    # SQLite round-trips datetimes as naive -- compare on wall-clock value, not tzinfo.
    assert item.detected_at.replace(tzinfo=_UTC) == now


def test_practice_evidence_counts_only_answered_attempts_and_computes_accuracy(db):
    assessment = _blueprint_and_assessment(db)
    question = Question(
        skill_tag="Kafka",
        type="mcq",
        difficulty="medium",
        prompt="...",
        expected_answer_or_rubric="...",
        explanation="...",
        source="ai_generated",
    )
    db.add(question)
    db.commit()
    db.refresh(question)

    db.add_all(
        [
            QuestionAttempt(assessment_id=assessment.id, question_id=question.id, student_answer="a", is_correct=True),
            QuestionAttempt(assessment_id=assessment.id, question_id=question.id, student_answer="b", is_correct=False),
            # Issued but never answered -- must not be counted.
            QuestionAttempt(assessment_id=assessment.id, question_id=question.id, student_answer=None, is_correct=None),
        ]
    )
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Kafka")
    assert len(view.items) == 1
    item = view.items[0]
    assert item.source == "practice"
    assert item.label == "Practice — 2 problems"
    assert item.score == 50.0


def test_project_evidence_flat_profile_project_matches_tech_stack(db):
    db.add(Project(profile_id=1, title="ETL Pipeline", description="A batch pipeline", tech_stack="Python, Spark"))
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Spark")
    assert len(view.items) == 1
    assert view.items[0].source == "project"
    assert view.items[0].label == "Project — ETL Pipeline"


def test_project_evidence_career_path_project_requires_completed_status(db):
    path = CareerPath(
        profile_id=1,
        role_family_key="sde",
        display_role_name="Software Engineer",
        is_primary=True,
        role_overview={},
    )
    db.add(path)
    db.commit()
    db.refresh(path)

    not_started = CareerPathProject(
        career_path_id=path.id,
        project_key="p1",
        title="Not Started Project",
        skills_demonstrated=["AWS"],
        technologies=[],
        order_index=0,
        status="not_started",
    )
    completed = CareerPathProject(
        career_path_id=path.id,
        project_key="p2",
        title="Completed Project",
        skills_demonstrated=["AWS"],
        technologies=[],
        order_index=1,
        status="completed",
    )
    db.add_all([not_started, completed])
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="AWS")
    assert len(view.items) == 1
    assert view.items[0].label == "Project — Completed Project"


def test_project_evidence_project_build_requires_completed_status(db):
    in_progress = ProjectBuild(
        profile_id=1, title="Realtime Pipeline", stack_summary="Kafka, Flink", status="in_progress"
    )
    completed = ProjectBuild(
        profile_id=1, title="Batch Warehouse Loader", stack_summary="Kafka, Airflow", status="completed",
        completed_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add_all([in_progress, completed])
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Kafka")
    assert len(view.items) == 1
    assert view.items[0].label == "Project — Batch Warehouse Loader"
    assert view.items[0].detected_at is not None


def test_interview_evidence_matches_based_on_tag_and_scores_from_evaluation(db):
    interview = Interview(profile_id=1, type="technical", mode="text", status="completed")
    db.add(interview)
    db.commit()
    db.refresh(interview)

    db.add(InterviewQuestion(interview_id=interview.id, question_text="Explain indexing.", order_index=0, based_on="SQL"))
    db.commit()

    now = datetime.datetime.now(_UTC)
    db.add(
        InterviewEvaluation(
            interview_id=interview.id,
            technical_correctness=0.8,
            relevance=0.8,
            completeness=0.8,
            reasoning=0.8,
            structure=0.8,
            clarity=0.8,
            role_jd_coverage=0.8,
            overall_score=0.84,
            feedback_text="Solid answer.",
            created_at=now,
        )
    )
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="SQL")
    assert len(view.items) == 1
    item = view.items[0]
    assert item.source == "interview"
    assert item.score == 84.0
    assert item.detected_at.replace(tzinfo=_UTC) == now


def test_verified_requires_three_sources_and_a_strong_score(db):
    now = datetime.datetime.now(_UTC)
    assessment = _blueprint_and_assessment(db)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"Python": 0.91},
            overall_score=0.91,
            priority_weaknesses=[],
            created_at=now,
        )
    )
    db.add(Project(profile_id=1, title="ETL Pipeline", description=None, tech_stack="Python"))
    resume = Resume(profile_id=1, file_path="x", raw_text="...", parse_status="completed")
    db.add(resume)
    db.commit()
    db.refresh(resume)
    db.add(
        ResumeAnalysis(
            resume_id=resume.id,
            extracted_skills=["Python"],
            extracted_education=[],
            extracted_experience=[],
            extracted_projects=[],
            extracted_certifications=[],
            ats_score=80.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Python")
    assert len(view.items) == 3  # assessment, project, resume
    assert view.state == "verified"
    assert view.confidence == 0.9


def test_three_sources_without_a_strong_score_is_strong_not_verified(db):
    assessment = _blueprint_and_assessment(db)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"Python": 0.5},  # below the strong-score threshold
            overall_score=0.5,
            priority_weaknesses=[],
        )
    )
    db.add(Project(profile_id=1, title="ETL Pipeline", description=None, tech_stack="Python"))
    resume = Resume(profile_id=1, file_path="x", raw_text="...", parse_status="completed")
    db.add(resume)
    db.commit()
    db.refresh(resume)
    db.add(
        ResumeAnalysis(
            resume_id=resume.id,
            extracted_skills=["Python"],
            extracted_education=[],
            extracted_experience=[],
            extracted_projects=[],
            extracted_certifications=[],
            ats_score=80.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Python")
    assert len(view.items) == 3
    assert view.state == "strong"
    assert view.confidence == 0.7


def test_stale_when_every_dated_item_is_older_than_the_window(db):
    old = datetime.datetime.now(_UTC) - datetime.timedelta(days=200)
    assessment = _blueprint_and_assessment(db)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"Python": 0.91},
            overall_score=0.91,
            priority_weaknesses=[],
            created_at=old,
        )
    )
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Python")
    assert view.state == "stale"
    assert view.confidence == 0.3


def test_undated_evidence_never_triggers_staleness_on_its_own(db):
    # A plain profile Project has no timestamp -- it must not be marked stale just because
    # it's the only evidence and it's undated.
    db.add(Project(profile_id=1, title="ETL Pipeline", description=None, tech_stack="Python"))
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Python")
    assert view.state == "supported"


def test_last_verified_at_is_the_max_across_dated_items(db):
    earlier = datetime.datetime.now(_UTC) - datetime.timedelta(days=10)
    later = datetime.datetime.now(_UTC) - datetime.timedelta(days=1)

    assessment = _blueprint_and_assessment(db)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={"Python": 0.6},
            overall_score=0.6,
            priority_weaknesses=[],
            created_at=earlier,
        )
    )
    db.commit()

    interview = Interview(profile_id=1, type="technical", mode="text", status="completed")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    db.add(InterviewQuestion(interview_id=interview.id, question_text="...", order_index=0, based_on="Python"))
    db.commit()
    db.add(
        InterviewEvaluation(
            interview_id=interview.id,
            technical_correctness=0.6,
            relevance=0.6,
            completeness=0.6,
            reasoning=0.6,
            structure=0.6,
            clarity=0.6,
            role_jd_coverage=0.6,
            overall_score=0.6,
            feedback_text="ok",
            created_at=later,
        )
    )
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="Python")
    assert view.last_verified_at.replace(tzinfo=_UTC) == later


def test_token_boundary_match_does_not_false_positive(db):
    # "R" must not match inside a resume/project mention of "Docker" (skill_present's own
    # documented guarantee -- confirmed here at the evidence-engine level, not just in
    # skill_matching's own tests).
    db.add(Project(profile_id=1, title="Container Demo", description=None, tech_stack="Docker"))
    db.commit()

    view = get_skill_evidence(db, profile_id=1, skill="R")
    assert view.items == []
    assert view.state == "claimed"


def test_get_all_skill_evidence_preserves_input_order(db):
    views = get_all_skill_evidence(db, profile_id=1, skills=["Kafka", "Python", "SQL"])
    assert [v.skill for v in views] == ["Kafka", "Python", "SQL"]
    assert all(v.state == "claimed" for v in views)


def test_get_all_skill_evidence_query_count_does_not_scale_with_skill_count(db):
    """Locks in the Phase 15 N+1 fix: every source is fetched once per profile (see
    _fetch_profile_sources), not once per skill -- so checking 5 skills must issue the exact
    same number of queries as checking 1, never 5x."""
    assessment = _blueprint_and_assessment(db)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id, skill_scores={"Python": 0.8}, overall_score=0.8, priority_weaknesses=[]
        )
    )
    db.commit()

    engine = db.get_bind()
    queries = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        get_all_skill_evidence(db, profile_id=1, skills=["Python"])
        one_skill_query_count = len(queries)

        queries.clear()
        get_all_skill_evidence(db, profile_id=1, skills=["Python", "SQL", "Kafka", "AWS", "Docker"])
        five_skill_query_count = len(queries)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert one_skill_query_count > 0
    assert five_skill_query_count == one_skill_query_count
