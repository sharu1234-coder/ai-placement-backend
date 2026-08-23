import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.certification import Certification
from app.models.education import Education
from app.models.experience import Experience
from app.models.interview import Interview
from app.models.interview_answer import InterviewAnswer
from app.models.interview_evaluation import InterviewEvaluation
from app.models.interview_question import InterviewQuestion
from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.question import Question
from app.models.question_attempt import QuestionAttempt
from app.models.requirement import Requirement
from app.models.roadmap import Roadmap
from app.models.roadmap_task import RoadmapTask
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.skill import Skill
from app.models.skill_gap import SkillGap
from app.models.user import User
from app.services.account_service import delete_account
from app.storage.provider import StorageProvider


class RecordingStorage(StorageProvider):
    """Records delete() calls instead of touching real storage -- lets tests assert the
    right keys were targeted without needing a filesystem or S3 backend."""

    def __init__(self):
        self.deleted_keys = []

    def save(self, key, data):
        return key

    def delete(self, key):
        self.deleted_keys.append(key)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        # SQLite ignores FKs by default -- this makes the test genuinely verify the
        # children-before-parents delete order, the same way a real Postgres deployment
        # (which does enforce FKs) would reject an out-of-order delete.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_local()
    yield session
    session.close()


def _seed_full_account(db, email: str):
    user = User(email=email, password_hash="hashed")
    db.add(user)
    db.commit()
    db.refresh(user)

    profile = StudentProfile(user_id=user.id, full_name="Jane Doe")
    db.add(profile)
    db.commit()
    db.refresh(profile)

    db.add(Education(profile_id=profile.id, institution="IIT Bombay"))
    db.add(Certification(profile_id=profile.id, name="AWS Certified"))
    db.add(Experience(profile_id=profile.id, company="Acme Corp"))
    db.add(Skill(profile_id=profile.id, name="Python"))
    db.add(Project(profile_id=profile.id, title="Portfolio site"))
    db.commit()

    resume = Resume(
        profile_id=profile.id, file_path="resumes/1_abc.pdf", raw_text="raw", parse_status="completed"
    )
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
            ats_score=70.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )

    jd = JobDescription(profile_id=profile.id, raw_text="JD text", source="paste")
    db.add(jd)
    db.commit()
    db.refresh(jd)
    db.add(Requirement(jd_id=jd.id, category="skill", value="Python", is_explicit=True, importance="required"))

    job_match = JobMatch(
        jd_id=jd.id, profile_id=profile.id, matching_skills=["Python"], missing_skills=[], match_score=80.0
    )
    db.add(job_match)
    db.commit()
    db.refresh(job_match)
    db.add(SkillGap(job_match_id=job_match.id, skill_name="SQL", gap_severity="high", source="jd_match"))

    blueprint = AssessmentBlueprint(profile_id=profile.id, jd_id=jd.id, sections=[{"skill": "Python"}])
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)

    assessment = Assessment(profile_id=profile.id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id, skill_scores={"Python": 80.0}, overall_score=80.0, priority_weaknesses=[]
        )
    )

    question = Question(
        skill_tag="Python",
        type="mcq",
        difficulty="medium",
        prompt="What is a list?",
        expected_answer_or_rubric="A mutable sequence",
        explanation="...",
    )
    db.add(question)
    db.commit()
    db.refresh(question)
    db.add(QuestionAttempt(assessment_id=assessment.id, question_id=question.id, student_answer="A mutable sequence"))

    interview = Interview(profile_id=profile.id, type="technical", mode="text", jd_id=jd.id, status="completed")
    db.add(interview)
    db.commit()
    db.refresh(interview)
    interview_question = InterviewQuestion(
        interview_id=interview.id, question_text="Tell me about yourself", order_index=1, based_on="HR"
    )
    db.add(interview_question)
    db.commit()
    db.refresh(interview_question)
    db.add(InterviewAnswer(interview_question_id=interview_question.id, answer_text="I am...", transcribed=False))
    db.add(
        InterviewEvaluation(
            interview_id=interview.id,
            technical_correctness=8.0,
            relevance=8.0,
            completeness=8.0,
            reasoning=8.0,
            structure=8.0,
            clarity=8.0,
            role_jd_coverage=8.0,
            overall_score=8.0,
            feedback_text="Good",
        )
    )

    roadmap = Roadmap(profile_id=profile.id, jd_id=jd.id, duration_days=30, status="active")
    db.add(roadmap)
    db.commit()
    db.refresh(roadmap)
    db.add(
        RoadmapTask(
            roadmap_id=roadmap.id,
            day_number=1,
            task_type="learn",
            skill_tag="Python",
            title="Learn lists",
            description="Study list operations",
            due_date=datetime.date.today(),
        )
    )

    db.commit()
    return user, profile, resume, question


def test_delete_account_removes_every_owned_row(db_session):
    user, profile, resume, question = _seed_full_account(db_session, "delete-me@example.com")
    resume_file_path = resume.file_path  # read before delete_account expires this row
    storage = RecordingStorage()

    delete_account(db_session, user, storage)

    assert db_session.get(User, user.id) is None
    assert db_session.get(StudentProfile, profile.id) is None
    assert db_session.query(Education).count() == 0
    assert db_session.query(Certification).count() == 0
    assert db_session.query(Experience).count() == 0
    assert db_session.query(Skill).count() == 0
    assert db_session.query(Project).count() == 0
    assert db_session.query(Resume).count() == 0
    assert db_session.query(ResumeAnalysis).count() == 0
    assert db_session.query(JobDescription).count() == 0
    assert db_session.query(Requirement).count() == 0
    assert db_session.query(JobMatch).count() == 0
    assert db_session.query(SkillGap).count() == 0
    assert db_session.query(AssessmentBlueprint).count() == 0
    assert db_session.query(Assessment).count() == 0
    assert db_session.query(AssessmentResult).count() == 0
    assert db_session.query(QuestionAttempt).count() == 0
    assert db_session.query(Interview).count() == 0
    assert db_session.query(InterviewQuestion).count() == 0
    assert db_session.query(InterviewAnswer).count() == 0
    assert db_session.query(InterviewEvaluation).count() == 0
    assert db_session.query(Roadmap).count() == 0
    assert db_session.query(RoadmapTask).count() == 0

    # The global question bank is shared across all users -- must never be touched.
    assert db_session.get(Question, question.id) is not None

    assert storage.deleted_keys == [resume_file_path]


def test_delete_account_with_no_profile_still_deletes_user(db_session):
    user = User(email="no-profile@example.com", password_hash="hashed")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    delete_account(db_session, user, RecordingStorage())

    assert db_session.get(User, user.id) is None


def test_delete_account_does_not_touch_other_users_data(db_session):
    other_user, other_profile, other_resume, _ = _seed_full_account(db_session, "other@example.com")

    victim = User(email="victim@example.com", password_hash="hashed")
    db_session.add(victim)
    db_session.commit()
    db_session.refresh(victim)
    db_session.add(StudentProfile(user_id=victim.id, full_name="Victim"))
    db_session.commit()

    delete_account(db_session, victim, RecordingStorage())

    assert db_session.get(User, other_user.id) is not None
    assert db_session.get(StudentProfile, other_profile.id) is not None
    assert db_session.query(Resume).filter(Resume.id == other_resume.id).count() == 1
