import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.placement_path import GeneratedPlacementPathExplanations, GeneratedTopicExplanation
from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.services import placement_path_service as svc
from app.services.skill_matching import NoResumeAnalysisError

PROFILE_ID = 1


class FakeAIProvider(AIProvider):
    def __init__(self, fail: bool = False):
        self.fail = fail

    def generate_structured(self, prompt, schema):
        if self.fail:
            raise AIGenerationError("simulated failure")
        return GeneratedPlacementPathExplanations(
            explanations=[GeneratedTopicExplanation(topic_key="sql_basics", why_it_matters="Matters.")]
        )

    def generate_text(self, prompt):
        return "fake"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _insert_resume(db, skills):
    resume = Resume(profile_id=PROFILE_ID, file_path="x.pdf", raw_text="text", parse_status="completed")
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
    return resume


def test_generate_raises_without_resume(db):
    with pytest.raises(NoResumeAnalysisError):
        svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())


def test_generate_raises_when_nothing_placement_relevant(db):
    _insert_resume(db, ["Cooking", "Photography"])
    with pytest.raises(svc.NoPlacementRelevantSkillsError):
        svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())


def test_generate_creates_deterministic_structure(db):
    _insert_resume(db, ["SQL", "Python"])
    result = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    assert result.total_topics > 0
    assert result.completed_topics == 0


def test_practice_blueprint_is_idempotent(db):
    _insert_resume(db, ["SQL"])
    path = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    topic_id = path.stages[0].topics[0].id

    first = svc.get_or_create_practice_blueprint(db, PROFILE_ID, topic_id)
    second = svc.get_or_create_practice_blueprint(db, PROFILE_ID, topic_id)
    assert first == second


def test_practice_blueprint_scoped_to_topics_own_skill_tag(db):
    _insert_resume(db, ["SQL"])
    path = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    topic = path.stages[0].topics[0]

    blueprint_id = svc.get_or_create_practice_blueprint(db, PROFILE_ID, topic.id)
    blueprint = db.get(AssessmentBlueprint, blueprint_id)
    assert blueprint.sections[0]["skill"] == topic.skill_tag
    assert blueprint.source == "placement_path"


def test_reconciliation_picks_best_of_multiple_retry_attempts(db):
    _insert_resume(db, ["SQL"])
    path = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    topic_id = path.stages[0].topics[0].id
    blueprint_id = svc.get_or_create_practice_blueprint(db, PROFILE_ID, topic_id)

    # First attempt: failed (below threshold).
    failed_assessment = Assessment(profile_id=PROFILE_ID, blueprint_id=blueprint_id, status="completed")
    db.add(failed_assessment)
    db.commit()
    db.refresh(failed_assessment)
    db.add(AssessmentResult(assessment_id=failed_assessment.id, skill_scores={}, overall_score=0.3, priority_weaknesses=[]))
    db.commit()

    reconciled = svc.get_current_placement_path(db, PROFILE_ID)
    topic = next(t for s in reconciled.stages for t in s.topics if t.id == topic_id)
    assert topic.status != "completed"

    # Second (retry) attempt: passes the threshold.
    passed_assessment = Assessment(profile_id=PROFILE_ID, blueprint_id=blueprint_id, status="completed")
    db.add(passed_assessment)
    db.commit()
    db.refresh(passed_assessment)
    db.add(
        AssessmentResult(
            assessment_id=passed_assessment.id, skill_scores={}, overall_score=0.9, priority_weaknesses=[]
        )
    )
    db.commit()

    reconciled = svc.get_current_placement_path(db, PROFILE_ID)
    topic = next(t for s in reconciled.stages for t in s.topics if t.id == topic_id)
    assert topic.status == "completed"
    assert topic.completed_via == "assessment"


def test_reconciliation_does_not_complete_below_threshold(db):
    _insert_resume(db, ["SQL"])
    path = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    topic_id = path.stages[0].topics[0].id
    blueprint_id = svc.get_or_create_practice_blueprint(db, PROFILE_ID, topic_id)

    assessment = Assessment(profile_id=PROFILE_ID, blueprint_id=blueprint_id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            skill_scores={},
            overall_score=svc.TOPIC_PASS_THRESHOLD - 0.01,
            priority_weaknesses=[],
        )
    )
    db.commit()

    reconciled = svc.get_current_placement_path(db, PROFILE_ID)
    topic = next(t for s in reconciled.stages for t in s.topics if t.id == topic_id)
    assert topic.status != "completed"


def test_manual_complete_rejected_for_practiceable_topic_before_any_practice(db):
    _insert_resume(db, ["SQL"])
    path = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    topic_id = path.stages[0].topics[0].id
    # Deliberately never called get_or_create_practice_blueprint -- practice_blueprint_id is
    # still None, but the topic's catalog definition still has an assessment_type, so manual
    # completion must be rejected regardless.
    with pytest.raises(svc.ManualCompletionNotAllowedError):
        svc.mark_topic_complete_manual(db, PROFILE_ID, topic_id)


def test_start_topic_rejected_when_locked(db):
    _insert_resume(db, ["SQL"])
    path = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    sql_topics = [t for s in path.stages for t in s.topics if t.track_key == "sql"]
    locked_topic = next(t for t in sql_topics if t.status == "locked")
    with pytest.raises(svc.TopicLockedError):
        svc.start_topic(db, PROFILE_ID, locked_topic.id)


def test_generate_with_failing_ai_still_succeeds_with_fallback_text(db):
    _insert_resume(db, ["SQL"])
    result = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider(fail=True))
    all_topics = [t for s in result.stages for t in s.topics]
    assert all(t.why_it_matters for t in all_topics)


def test_regenerate_preserves_completed_topic_status(db):
    _insert_resume(db, ["SQL"])
    first_path = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    interview_topic = next(t for s in first_path.stages for t in s.topics if t.track_key == "interview_prep")
    svc.mark_topic_complete_manual(db, PROFILE_ID, interview_topic.id)

    second_path = svc.generate_placement_path(db, PROFILE_ID, FakeAIProvider())
    assert second_path.id != first_path.id
    new_interview_topic = next(
        t for s in second_path.stages for t in s.topics if t.track_key == "interview_prep"
    )
    assert new_interview_topic.topic_key == interview_topic.topic_key
    assert new_interview_topic.status == "completed"
