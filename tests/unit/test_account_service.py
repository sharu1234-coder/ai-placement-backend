import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.arena_attempt import ArenaAttempt
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.career_path_phase import CareerPathPhase
from app.models.career_path_project import CareerPathProject
from app.models.career_path_skill_item import CareerPathSkillItem
from app.models.career_strategy_target import CareerStrategyTarget
from app.models.certification import Certification
from app.models.conversation import Conversation, ConversationTurn
from app.models.deletion_job import DeletionJob
from app.models.deletion_request import DeletionRequest
from app.models.education import Education
from app.models.evidence_state_snapshot import EvidenceStateSnapshot
from app.models.experience import Experience
from app.models.external_evidence_item import ExternalEvidenceItem
from app.models.external_profile_snapshot import ExternalProfileSnapshot
from app.models.interview import Interview
from app.models.interview_answer import InterviewAnswer
from app.models.interview_evaluation import InterviewEvaluation
from app.models.interview_question import InterviewQuestion
from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.job_preparation_plan import JobPreparationPlan
from app.models.mission import Mission
from app.models.notification_event import NotificationEvent
from app.models.password_reset_token import PasswordResetToken
from app.models.placement_alarm_challenge import PlacementAlarmChallenge
from app.models.placement_alarm_question_attempt import PlacementAlarmQuestionAttempt
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.project_build import ProjectBuild
from app.models.project_build_stage import ProjectBuildStage
from app.models.provider_connection import ProviderConnection
from app.models.provider_oauth_state import ProviderOAuthState
from app.models.question import Question
from app.models.question_attempt import QuestionAttempt
from app.models.readiness_snapshot import ReadinessSnapshot
from app.models.recruiter_attack_session import RecruiterAttackSession
from app.models.recruiter_attack_turn import RecruiterAttackTurn
from app.models.requirement import Requirement
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.resume_ats_check import ResumeAtsCheck
from app.models.roadmap import Roadmap
from app.models.roadmap_task import RoadmapTask
from app.models.role import Role
from app.models.skill import Skill
from app.models.skill_gap import SkillGap
from app.models.subscription import Subscription
from app.models.user import User
from app.models.weakness_record import WeaknessRecord
from app.services.account_service import delete_account
from app.storage.provider import StorageError, StorageProvider
from app.workers import deletion_worker


class RecordingStorage(StorageProvider):
    """Records delete() calls instead of touching real storage -- lets tests assert the
    right keys were targeted without needing a filesystem or S3 backend."""

    def __init__(self):
        self.deleted_keys = []

    def save(self, key, data):
        return key

    def delete(self, key):
        self.deleted_keys.append(key)

    def load(self, key):
        raise NotImplementedError("RecordingStorage does not back real file content")


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

    db.add(JobPreparationPlan(
        jd_id=jd.id, profile_id=profile.id, job_summary={}, required_skills=[],
        preparation_gaps=[], preparation_plan=[], interview_prep=[],
    ))

    db.add(ResumeAtsCheck(
        resume_id=resume.id, profile_id=profile.id, overall_score=80, score_category="good",
        parsing_score=80, structure_score=80, content_score=80, skills_score=80,
        experience_score=80, projects_score=80, contact_score=80, readability_score=80,
        category_breakdown=[], section_analysis=[], issues=[], recommendations=[],
        ai_summary="Looks solid.",
    ))

    # Career Path family -- phase/skill-item/project each reference career_path_id;
    # skill_item additionally references the SAME blueprint created above (nullable), so this
    # exercises the exact ordering the account-deletion fix depends on.
    career_path = CareerPath(
        profile_id=profile.id, role_family_key="data_analyst", display_role_name="Data Analyst",
        role_overview={},
    )
    db.add(career_path)
    db.commit()
    db.refresh(career_path)
    db.add(CareerPathPhase(career_path_id=career_path.id, phase_key="foundations", order_index=0, title="Foundations"))
    db.add(CareerPathSkillItem(
        career_path_id=career_path.id, phase_key="foundations", skill_key="sql", title="SQL",
        order_index=0, practice_blueprint_id=blueprint.id,
    ))
    db.add(CareerPathProject(
        career_path_id=career_path.id, project_key="dashboard", title="Sales Dashboard",
        skills_demonstrated=["SQL"], technologies=["Power BI"], order_index=0,
    ))

    # Placement Path (Roadmap's topic-tree sibling, NOT the Roadmap seeded above) -- its
    # nullable source_resume_id points at the SAME resume created earlier, which is exactly
    # the ordering the account-deletion fix depends on (placement_paths before resumes).
    placement_path = PlacementPath(profile_id=profile.id, source_resume_id=resume.id)
    db.add(placement_path)
    db.commit()
    db.refresh(placement_path)
    db.add(PlacementPathTopic(
        path_id=placement_path.id, stage="foundation", track_key="sql", topic_key="sql_joins",
        order_in_track=0, title="SQL Joins", skill_tag="SQL", practice_blueprint_id=blueprint.id,
    ))

    challenge = PlacementAlarmChallenge(profile_id=profile.id, client_alarm_id="alarm-1")
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    db.add(PlacementAlarmQuestionAttempt(challenge_id=challenge.id, question_id=question.id, question_index=0))

    db.add(Subscription(
        profile_id=profile.id, plan="1_month", amount_paise=19_900, currency="INR",
        razorpay_order_id=f"order_{profile.id}",
    ))

    db.add(PasswordResetToken(
        user_id=user.id, token_hash="deadbeef",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=15),
    ))

    # The 10 tables the account-deletion FK-graph audit found delete_account never visited --
    # every one of these has a real FK chain back to student_profiles.id (some via each
    # other), so this is exactly what turns this test from "misses the same blind spot as the
    # implementation" into a real regression guard for that fix.
    db.add(ArenaAttempt(profile_id=profile.id, challenge_key="sql_basics", assessment_id=assessment.id))
    db.add(Mission(
        profile_id=profile.id, mission_date=datetime.date.today(), title="Fix SQL Window Functions",
        action_type="practice", assessment_id=assessment.id,
    ))
    db.add(EvidenceStateSnapshot(profile_id=profile.id, skill_key="sql", state="strong"))
    db.add(NotificationEvent(
        profile_id=profile.id, kind="mission_completed", title="Mission complete", body="Nice work!",
    ))
    db.add(ReadinessSnapshot(
        profile_id=profile.id, role_family_key="data_analyst", snapshot_date=datetime.date.today(),
        evidence_coverage_percent=78, per_skill_confidence_percent={"sql": 80},
    ))
    db.add(WeaknessRecord(profile_id=profile.id, skill_key="interview:technical_correctness", last_score=0.4))

    project_build = ProjectBuild(profile_id=profile.id, title="Real-Time Data Pipeline")
    db.add(project_build)
    db.commit()
    db.refresh(project_build)
    db.add(ProjectBuildStage(
        build_id=project_build.id, stage_key="architecture", order_index=0, guidance_text="Design the schema.",
    ))

    # source_project_build_id nullably references the SAME build above -- exactly the ordering
    # (recruiter_attack_sessions before project_builds) the account-deletion fix depends on.
    recruiter_session = RecruiterAttackSession(
        profile_id=profile.id, source_type="project_build", source_label="Real-Time Data Pipeline",
        source_project_build_id=project_build.id,
    )
    db.add(recruiter_session)
    db.commit()
    db.refresh(recruiter_session)
    db.add(RecruiterAttackTurn(
        session_id=recruiter_session.id, order_index=0, question_text="Why Spark over Flink?",
        based_on="Scaling claim",
    ))

    # Professional Presence. The two evidence tables hang off the connection rather than off
    # the profile, so they exercise the subquery-shaped delete rather than the profile filter --
    # and the stored access token is what makes this more than a referential-integrity check:
    # a connection left behind here is a live third-party credential belonging to an account
    # that no longer exists, which nobody can reach to revoke.
    connection = ProviderConnection(
        profile_id=profile.id,
        provider="github",
        provider_user_id="583231",
        provider_username="octocat",
        access_token_encrypted="gAAAAA-not-a-real-token-ciphertext",
        granted_scopes="",
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    db.add(ExternalEvidenceItem(
        connection_id=connection.id, skill="Python", signal_kind="repo_primary_language",
        source_ref="github:repo:octocat/hello-world",
        observed_at=datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),
    ))
    db.add(ExternalProfileSnapshot(
        connection_id=connection.id, sequence=1, fingerprint="a" * 64, payload={"public_repos": 4},
    ))
    db.add(ProviderOAuthState(
        state_hash="b" * 64, profile_id=profile.id, provider="github",
        code_verifier_encrypted="gAAAAA-not-a-real-verifier-ciphertext",
        expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc),
    ))

    # Career Strategy comparison target. Needs a real Role row because role_id is a
    # non-nullable FK into the shared catalog -- and that catalog must survive the delete,
    # so it is returned for the caller to assert on alongside the shared question bank.
    role = Role(name="Data Engineer", category="Data", description="Builds data pipelines")
    db.add(role)
    db.commit()
    db.refresh(role)
    db.add(CareerStrategyTarget(profile_id=profile.id, role_id=role.id, position=0))

    db.commit()
    return user, profile, resume, question, role


def test_delete_account_removes_every_owned_row(db_session):
    user, profile, resume, question, role = _seed_full_account(db_session, "delete-me@example.com")
    resume_file_path = resume.file_path  # read before delete_account expires this row
    storage = RecordingStorage()

    delete_account(db_session, user, storage)

    # Phase 1 of the outbox keeps the user row until the objects are erased, and revokes
    # access instead of deleting it. Everything else still goes in this transaction.
    assert db_session.get(User, user.id) is not None
    assert db_session.get(User, user.id).is_active is False
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
    assert db_session.query(JobPreparationPlan).count() == 0
    assert db_session.query(ResumeAtsCheck).count() == 0
    assert db_session.query(CareerPath).count() == 0
    assert db_session.query(CareerPathPhase).count() == 0
    assert db_session.query(CareerPathSkillItem).count() == 0
    assert db_session.query(CareerPathProject).count() == 0
    assert db_session.query(PlacementPath).count() == 0
    assert db_session.query(PlacementPathTopic).count() == 0
    assert db_session.query(PlacementAlarmChallenge).count() == 0
    assert db_session.query(PlacementAlarmQuestionAttempt).count() == 0
    assert db_session.query(Subscription).count() == 0
    assert db_session.query(PasswordResetToken).count() == 0
    assert db_session.query(ProviderConnection).count() == 0
    assert db_session.query(ExternalEvidenceItem).count() == 0
    assert db_session.query(ExternalProfileSnapshot).count() == 0
    assert db_session.query(ProviderOAuthState).count() == 0

    assert db_session.query(CareerStrategyTarget).count() == 0

    # The global question bank is shared across all users -- must never be touched.
    assert db_session.get(Question, question.id) is not None
    # Nor is the role catalog, even though a deleted career_strategy_targets row pointed at it.
    assert db_session.get(Role, role.id) is not None

    # The key moved from the resume row into a durable job, in this same transaction, and
    # no storage call was made: erasure is the worker obligation now.
    assert storage.deleted_keys == []
    assert [job.storage_key for job in db_session.query(DeletionJob).all()] == [
        resume_file_path
    ]
    assert db_session.query(DeletionRequest).one().status == "pending"


def test_delete_account_with_no_profile_still_deletes_user(db_session):
    user = User(email="no-profile@example.com", password_hash="hashed")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    delete_account(db_session, user, RecordingStorage())

    assert db_session.get(User, user.id) is None


def test_delete_account_does_not_touch_other_users_data(db_session):
    other_user, other_profile, other_resume, _, _ = _seed_full_account(db_session, "other@example.com")

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


def test_delete_account_succeeds_for_a_student_with_a_career_strategy_target(db_session):
    """F-01 regression, reduced to the smallest case that reproduced it.

    career_strategy_targets.profile_id is NOT NULL with no ondelete, so before the fix the
    `db.delete(profile)` at the end of delete_account raised IntegrityError during autoflush
    the moment a student had ever added one comparison role -- a 500 from DELETE /users/me,
    and an account the owner could never delete. Premium-only, so it hit exactly the students
    who had paid.

    Deliberately NOT part of the full-account seed alone: that test would have caught this
    too, but only because someone remembered to add the row. This one names the defect, so a
    future reordering of delete_account that reintroduces it fails with an obvious test name.
    """
    user = User(email="strategy-target@example.com", password_hash="hashed")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    profile = StudentProfile(user_id=user.id, full_name="Priya")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    role = Role(name="ML Engineer", category="Data", description="Trains models")
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)

    target = CareerStrategyTarget(profile_id=profile.id, role_id=role.id, position=0)
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    target_id = target.id

    delete_account(db_session, user, RecordingStorage())

    assert db_session.get(User, user.id) is None
    assert db_session.get(StudentProfile, profile.id) is None
    assert db_session.get(CareerStrategyTarget, target_id) is None
    # The role is shared catalog data -- a student deleting their account must not remove a
    # role every other student compares against.
    assert db_session.get(Role, role.id) is not None


def test_delete_account_succeeds_for_a_student_who_held_a_voice_session(db_session):
    """The unified conversation store, in the same shape as the F-01 regression above.

    `conversations.profile_id` is NOT NULL with no ondelete, and `delete_account` did not
    mention the table at all when it was added with the realtime voice agent. The
    consequences were two, and the second is the serious one:

    1. a student's spoken transcripts -- career-coach conversations and mock-interview
       answers, in their own words -- would have survived the deletion of their account;
    2. they would not actually have survived it, because `db.delete(profile)` at the end of
       `delete_account` would raise IntegrityError first. Account deletion was broken outright
       for every student who had ever held a voice session, which after the voice agent
       shipped is the ordinary case rather than an edge one.

    The source-parity guard below catches the omission, but only by grepping the function for
    a model name -- it cannot tell whether the rows genuinely go, or whether the delete is
    ordered early enough to precede `interviews`. This test seeds both tables and asserts the
    outcome, so the ordering is covered too.
    """
    user = User(email="voice-session@example.com", password_hash="hashed")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    profile = StudentProfile(user_id=user.id, full_name="Arun")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    # A mock-interview conversation, because that is the variant that also carries an
    # interview_id -- the reference that forces conversations to be deleted before interviews.
    interview = Interview(profile_id=profile.id, type="technical", mode="voice", status="completed")
    db_session.add(interview)
    db_session.commit()
    db_session.refresh(interview)

    conversation = Conversation(
        profile_id=profile.id,
        kind="mock_interview",
        session_key="voice-deletion-test",
        interview_id=interview.id,
        status="completed",
    )
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)

    turn = ConversationTurn(
        conversation_id=conversation.id,
        seq=1,
        role="user",
        # NOT NULL: the gateway's own turn id, which with (conversation_id, role) is the
        # unique key that makes a replayed turn a no-op rather than a duplicate.
        turn_id="turn-1",
        text="I led the migration of our billing service.",
    )
    db_session.add(turn)
    db_session.commit()
    db_session.refresh(turn)
    conversation_id, turn_id, interview_id = conversation.id, turn.id, interview.id

    delete_account(db_session, user, RecordingStorage())

    assert db_session.get(User, user.id) is None
    assert db_session.get(StudentProfile, profile.id) is None
    # The transcript itself, which is the student's own speech and the whole point of erasure.
    assert db_session.get(ConversationTurn, turn_id) is None
    assert db_session.get(Conversation, conversation_id) is None
    # Counted rather than fetched: `delete_account` removes interviews with a bulk delete and
    # `synchronize_session=False`, so the Interview this test seeded is still in the session's
    # identity map, and `get()` would try to refresh a row that no longer exists and raise
    # ObjectDeletedError instead of returning None. A fresh SELECT answers the real question.
    #
    # The ordering it covers: the conversation references this interview, so if conversations
    # were deleted after interviews, delete_account would already have failed above.
    assert db_session.query(Interview).filter(Interview.id == interview_id).count() == 0


def test_deleting_an_account_removes_the_jobs_that_student_typed_in(db_session):
    """Phase 4 made job_listings hold two kinds of row with opposite privacy, and account
    deletion has to tell them apart.

    A provider advert is public content many students share, so removing it would delete
    another student's saved job. A manually entered one is this student's own writing -- a
    company a senior named, a description pasted out of an email -- and leaving it behind
    would keep that text after they erased their account. It also carries a foreign key to
    student_profiles, so leaving it would make the deletion fail outright rather than merely
    leak.
    """
    from app.models.job_listing import JobListing
    from app.models.saved_job import SavedJob

    user = User(email="typed@example.com", password_hash="hashed")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    profile = StudentProfile(user_id=user.id, full_name="Priya")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    shared = JobListing(
        provider="adzuna",
        provider_job_id="adz-1",
        url="https://jobs.example.com/1",
        title="Data Engineer",
    )
    mine = JobListing(
        provider="manual",
        provider_job_id=f"{profile.id}:abc",
        url=None,
        title="Campus placement -- SDE",
        description="Told to me over email by my senior.",
        created_by_profile_id=profile.id,
    )
    db_session.add_all([shared, mine])
    db_session.commit()
    db_session.refresh(shared)
    db_session.refresh(mine)
    shared_id, mine_id = shared.id, mine.id
    db_session.add(SavedJob(profile_id=profile.id, listing_id=mine.id, status="interview"))
    db_session.add(SavedJob(profile_id=profile.id, listing_id=shared.id, status="applied"))
    db_session.commit()

    delete_account(db_session, user, RecordingStorage())

    assert db_session.get(JobListing, mine_id) is None, "the student's own text is gone"
    assert db_session.get(JobListing, shared_id) is not None, (
        "a provider advert is shared content and must survive one student leaving"
    )


# Tables that carry a student_profiles foreign key but are deliberately NOT deleted by
# profile, each with the reason. Anything else with such a key must be covered by
# delete_account, or the parity test below fails.
#
# Empty today, and kept as the documented escape hatch rather than removed: the alternative
# when a genuinely shared table gains a profile_id is editing the test's logic under time
# pressure, which is how an exception becomes an accident.
_DELETION_EXEMPT_TABLES: dict = {}


def test_every_profile_owned_model_is_covered_by_account_deletion():
    """F-10-adjacent parity guard for F-01's actual class of defect.

    F-01 was not a logic error -- delete_account's ordering was correct and its reasoning was
    written down. It was an omission: a new profile-owned table landed and nobody added it
    here. No test could see that, because every deletion test asserts on tables it already
    knows about, so the one table nobody thought of is exactly the one nobody asserts on.

    This inverts it. Instead of listing what should be deleted, it asks SQLAlchemy which
    models actually carry a student_profiles foreign key, and requires each to appear in
    delete_account. Adding a profile-owned table without touching account deletion now fails
    here rather than in production.

    Deliberately checks the source for a reference rather than running a deletion: proving a
    table is emptied would need a seeded row per model (63 of them, several with their own
    non-nullable parents), and that fixture would itself be the thing nobody updates. A source
    reference is a weaker assertion but a maintainable one -- and the seeded end-to-end
    deletion test above still proves the rows genuinely go.
    """
    import inspect

    from app.services import account_service

    source = inspect.getsource(account_service.delete_account)

    missing = []
    for mapper in Base.registry.mappers:
        model = mapper.class_
        table = mapper.local_table
        if table is None:
            continue
        owns_profile = any(
            fk.column.table.name == "student_profiles"
            for column in table.columns
            for fk in column.foreign_keys
        )
        if not owns_profile:
            continue
        if table.name in _DELETION_EXEMPT_TABLES:
            continue
        if model.__name__ not in source:
            missing.append((model.__name__, table.name))

    assert not missing, (
        "These models have a student_profiles foreign key but are never referenced in "
        "account_service.delete_account, so a student's rows would survive deleting their "
        "account (or, for a NOT NULL key, make deletion fail outright): "
        + ", ".join(f"{name} ({tbl})" for name, tbl in sorted(missing))
        + ". Add them to delete_account in the correct topological position, or add the table "
        "to _DELETION_EXEMPT_TABLES with the reason it is shared rather than user-owned."
    )


def test_the_parity_guard_actually_detects_an_uncovered_model():
    """Proves the guard above can fail -- a test that only ever passes is not a guard.

    Rather than temporarily breaking delete_account, this re-runs the same rule against a
    source string with one model's name removed, and asserts that model is reported.
    """
    import inspect

    from app.services import account_service

    source = inspect.getsource(account_service.delete_account)
    crippled = source.replace("CareerStrategyTarget", "SomethingElseEntirely")

    missing = []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        if not any(
            fk.column.table.name == "student_profiles"
            for column in table.columns
            for fk in column.foreign_keys
        ):
            continue
        if mapper.class_.__name__ not in crippled:
            missing.append(mapper.class_.__name__)

    assert "CareerStrategyTarget" in missing


# ---------------------------------------------------------------------------------------
# Finding 1: an erasure that did not happen must not be reported as one.
#
# Before this, `StorageProvider.delete` was allowed to return normally whatever happened --
# the S3 adapter caught every BotoCoreError/ClientError, logged a warning and returned -- and
# `delete_account` wrapped the call in `except Exception` as well, with the comment "File
# deletion is best-effort and never blocks the DB delete". It then committed. So a storage
# outage erased the rows holding the only copy of `file_path` while the object stayed in the
# bucket: an orphan nobody could find, belonging to a user told their data was gone.
# ---------------------------------------------------------------------------------------


class FailingStorage(RecordingStorage):
    """Storage whose delete genuinely fails, the way a permissions error or outage does."""

    def __init__(self, failing_keys=None):
        super().__init__()
        self.failing_keys = failing_keys
        self.attempts = []

    def delete(self, key):
        self.attempts.append(key)
        if self.failing_keys is None or key in self.failing_keys:
            raise StorageError("simulated storage outage")
        self.deleted_keys.append(key)


def test_a_storage_outage_cannot_orphan_the_file_or_block_the_account(db_session):
    """The same invariant as before, held by a different mechanism.

    This used to assert that a storage failure aborted the whole deletion. That held the
    invariant but made a user's ability to leave depend on a bucket being up. Phase 1 now
    makes no storage call at all, so an outage cannot abort anything, and the pointer survives
    in a `deletion_jobs` row instead of in `resumes`. What must never happen is unchanged: the
    last pointer to a stored object is never destroyed.
    """
    user, profile, resume, question, role = _seed_full_account(db_session, "orphan@example.com")
    resume_id = resume.id
    user_id = user.id
    key = resume.file_path

    storage = FailingStorage()
    delete_account(db_session, user, storage)  # a bucket that fails every call

    assert storage.attempts == [], "Phase 1 must not touch storage"
    assert db_session.get(Resume, resume_id) is None
    assert [job.storage_key for job in db_session.query(DeletionJob).all()] == [key], (
        "the key must survive the delete somewhere durable, or the object is unreachable"
    )
    revoked = db_session.get(User, user_id)
    assert revoked is not None and revoked.is_active is False, (
        "access is revoked even though the bucket is unreachable"
    )


def test_a_retry_after_a_storage_failure_completes(db_session):
    """Deletion is idempotent, so partial progress is real progress.

    The retry is the worker's now rather than the user's: the obligation is already durable,
    so nobody has to ask a second time.
    """
    user, profile, resume, question, role = _seed_full_account(db_session, "retry@example.com")
    user_id = user.id

    delete_account(db_session, user, FailingStorage())

    failing = FailingStorage()
    deletion_worker.run_once(db_session, failing)
    assert failing.attempts, "the worker must have tried"
    assert db_session.query(DeletionJob).one().status == "pending"
    assert db_session.get(User, user_id) is not None

    # Storage recovers. The same job finishes, and the last user row goes with it.
    job = db_session.query(DeletionJob).one()
    job.next_attempt_at = deletion_worker._now() - datetime.timedelta(seconds=1)
    job.locked_until = None
    db_session.commit()

    storage = RecordingStorage()
    deletion_worker.run_once(db_session, storage)

    assert db_session.get(User, user_id) is None
    assert len(storage.deleted_keys) == 1
    assert db_session.query(DeletionRequest).one().status == "done"


def test_every_object_is_attempted_before_giving_up(db_session):
    """One bad key must not strand the others: a retry should have less to do, not the same."""
    user, profile, resume, question, role = _seed_full_account(db_session, "many@example.com")
    extra = Resume(
        profile_id=profile.id,
        file_path="resumes/second-file.pdf",
        raw_text="raw",
        parse_status="completed",
    )
    db_session.add(extra)
    db_session.commit()
    first_key = resume.file_path

    storage = FailingStorage(failing_keys={"resumes/second-file.pdf"})
    delete_account(db_session, user, storage)

    # One job per object, so one bad key cannot strand the other.
    keys = {job.storage_key for job in db_session.query(DeletionJob).all()}
    assert keys == {first_key, "resumes/second-file.pdf"}

    deletion_worker.run_once(db_session, storage)
    assert len(storage.attempts) == 2, "both objects must be attempted, not just the first"
    assert first_key in storage.deleted_keys
    statuses = {job.storage_key: job.status for job in db_session.query(DeletionJob).all()}
    assert statuses[first_key] == "done"
    assert statuses["resumes/second-file.pdf"] == "pending", (
        "the failing object stays queued; the obligation is not abandoned"
    )
    assert db_session.query(DeletionRequest).one().status == "pending"


class TestStorageAdaptersReportFailure:
    """The adapters themselves, which is where the silence originated."""

    def test_local_storage_treats_an_absent_file_as_success(self, tmp_path, monkeypatch):
        from app.core.config import settings
        from app.storage.local_provider import LocalStorageProvider

        monkeypatch.setattr(settings, "upload_dir", str(tmp_path), raising=False)
        # Idempotency: a retry must not fail on what it already deleted.
        LocalStorageProvider().delete("nothing/here.pdf")

    def test_local_storage_raises_when_deletion_genuinely_fails(self, tmp_path, monkeypatch):
        from pathlib import Path

        from app.core.config import settings
        from app.storage.local_provider import LocalStorageProvider

        monkeypatch.setattr(settings, "upload_dir", str(tmp_path), raising=False)

        def boom(self, missing_ok=False):
            raise OSError("read-only file system")

        monkeypatch.setattr(Path, "unlink", boom)
        with pytest.raises(StorageError, match="Failed to delete"):
            LocalStorageProvider().delete("some/file.pdf")

    def test_s3_treats_a_missing_key_as_success(self):
        from botocore.exceptions import ClientError

        from app.storage.s3_provider import S3StorageProvider

        provider = S3StorageProvider.__new__(S3StorageProvider)
        provider.bucket = "b"

        class Client:
            def delete_object(self, **_):
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "DeleteObject")

        provider._client = Client()
        provider.delete("gone.pdf")  # must not raise

    def test_s3_raises_on_a_real_failure(self):
        from botocore.exceptions import ClientError

        from app.storage.s3_provider import S3StorageProvider

        provider = S3StorageProvider.__new__(S3StorageProvider)
        provider.bucket = "b"

        class Client:
            def delete_object(self, **_):
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")

        provider._client = Client()
        with pytest.raises(StorageError, match="Failed to delete"):
            provider.delete("still-there.pdf")

    def test_s3_raises_when_the_request_never_arrived(self):
        from botocore.exceptions import EndpointConnectionError

        from app.storage.s3_provider import S3StorageProvider

        provider = S3StorageProvider.__new__(S3StorageProvider)
        provider.bucket = "b"

        class Client:
            def delete_object(self, **_):
                raise EndpointConnectionError(endpoint_url="https://example.invalid")

        provider._client = Client()
        with pytest.raises(StorageError, match="Failed to delete"):
            provider.delete("unknown.pdf")
