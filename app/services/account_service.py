import logging

from sqlalchemy.orm import Session

from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.career_path_phase import CareerPathPhase
from app.models.career_path_project import CareerPathProject
from app.models.career_path_skill_item import CareerPathSkillItem
from app.models.certification import Certification
from app.models.education import Education
from app.models.experience import Experience
from app.models.interview import Interview
from app.models.interview_answer import InterviewAnswer
from app.models.interview_evaluation import InterviewEvaluation
from app.models.interview_question import InterviewQuestion
from app.models.job_description import JobDescription
from app.models.job_match import JobMatch
from app.models.job_preparation_plan import JobPreparationPlan
from app.models.password_reset_token import PasswordResetToken
from app.models.placement_alarm_challenge import PlacementAlarmChallenge
from app.models.placement_alarm_question_attempt import PlacementAlarmQuestionAttempt
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.question_attempt import QuestionAttempt
from app.models.requirement import Requirement
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.resume_ats_check import ResumeAtsCheck
from app.models.roadmap import Roadmap
from app.models.roadmap_task import RoadmapTask
from app.models.skill import Skill
from app.models.skill_gap import SkillGap
from app.models.subscription import Subscription
from app.models.user import User
from app.storage.provider import StorageProvider

logger = logging.getLogger(__name__)


def delete_account(db: Session, user: User, storage: StorageProvider) -> None:
    """PROJECT_HANDOFF.md section 6 / production-readiness item 5: erase every row this
    user's data touches, not just the `users` row -- a partial delete would leave orphaned
    personal data (resumes, interview transcripts, assessment history) behind under a
    User the account owner can no longer reach or delete themselves.

    Every table below is exclusively owned by one profile (never shared across users --
    e.g. a JobDescription is only ever created for the requesting profile, see
    jd_service.parse_job_description), so deleting by profile_id/derived ids here can never
    remove another user's data.

    Ordering is a real topological sort of this schema's FK graph (verified against every
    ForeignKey(...) in app/models/, not assumed), not just "children before parents" applied
    loosely -- several tables both reference and are referenced by others:
      - assessment_blueprints has a nullable FK to job_descriptions, so it must be deleted
        before job_descriptions, but only after everything that references IT
        (placement_path_topics, career_path_skill_items, assessments) is gone.
      - placement_paths has a nullable FK to resumes, so it must be deleted before resumes.
      - interviews, roadmaps, job_matches, job_preparation_plans, requirements, and
        assessment_blueprints all reference job_descriptions, so job_descriptions can only be
        deleted once every one of those is gone.
    This matters for a real, constraint-enforcing database (Postgres) -- SQLite's default
    unenforced FKs would silently tolerate a wrong order, which is exactly how a wrong order
    here could go undetected until it hits production.
    """
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()

    if profile is not None:
        pid = profile.id

        # -- Leaves: tables nothing else references -------------------------------------
        db.query(ResumeAnalysis).filter(
            ResumeAnalysis.resume_id.in_(db.query(Resume.id).filter(Resume.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(ResumeAtsCheck).filter(ResumeAtsCheck.profile_id == pid).delete(synchronize_session=False)
        db.query(PlacementPathTopic).filter(
            PlacementPathTopic.path_id.in_(db.query(PlacementPath.id).filter(PlacementPath.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(CareerPathPhase).filter(
            CareerPathPhase.career_path_id.in_(db.query(CareerPath.id).filter(CareerPath.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(CareerPathProject).filter(
            CareerPathProject.career_path_id.in_(db.query(CareerPath.id).filter(CareerPath.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(CareerPathSkillItem).filter(
            CareerPathSkillItem.career_path_id.in_(db.query(CareerPath.id).filter(CareerPath.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(AssessmentResult).filter(
            AssessmentResult.assessment_id.in_(db.query(Assessment.id).filter(Assessment.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(QuestionAttempt).filter(
            QuestionAttempt.assessment_id.in_(db.query(Assessment.id).filter(Assessment.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(InterviewAnswer).filter(
            InterviewAnswer.interview_question_id.in_(
                db.query(InterviewQuestion.id).filter(
                    InterviewQuestion.interview_id.in_(db.query(Interview.id).filter(Interview.profile_id == pid))
                )
            )
        ).delete(synchronize_session=False)
        db.query(InterviewEvaluation).filter(
            InterviewEvaluation.interview_id.in_(db.query(Interview.id).filter(Interview.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(RoadmapTask).filter(
            RoadmapTask.roadmap_id.in_(db.query(Roadmap.id).filter(Roadmap.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(SkillGap).filter(
            SkillGap.job_match_id.in_(db.query(JobMatch.id).filter(JobMatch.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(Requirement).filter(
            Requirement.jd_id.in_(db.query(JobDescription.id).filter(JobDescription.profile_id == pid))
        ).delete(synchronize_session=False)
        db.query(JobPreparationPlan).filter(JobPreparationPlan.profile_id == pid).delete(
            synchronize_session=False
        )
        db.query(PlacementAlarmQuestionAttempt).filter(
            PlacementAlarmQuestionAttempt.challenge_id.in_(
                db.query(PlacementAlarmChallenge.id).filter(PlacementAlarmChallenge.profile_id == pid)
            )
        ).delete(synchronize_session=False)

        # -- InterviewQuestion: deleted only after InterviewAnswer (above) is gone -------
        db.query(InterviewQuestion).filter(
            InterviewQuestion.interview_id.in_(db.query(Interview.id).filter(Interview.profile_id == pid))
        ).delete(synchronize_session=False)

        # -- Mid-level: referenced by the leaves above, and themselves reference resumes/
        # assessment_blueprints/job_descriptions below -- must go before those parents. ----
        db.query(PlacementPath).filter(PlacementPath.profile_id == pid).delete(synchronize_session=False)
        db.query(CareerPath).filter(CareerPath.profile_id == pid).delete(synchronize_session=False)
        db.query(Assessment).filter(Assessment.profile_id == pid).delete(synchronize_session=False)
        db.query(Interview).filter(Interview.profile_id == pid).delete(synchronize_session=False)
        db.query(Roadmap).filter(Roadmap.profile_id == pid).delete(synchronize_session=False)
        db.query(JobMatch).filter(JobMatch.profile_id == pid).delete(synchronize_session=False)

        # -- assessment_blueprints: must follow everything that references it
        # (placement_path_topics, career_path_skill_items, assessments -- all deleted above)
        # and precede job_descriptions (which it nullably references). ---------------------
        db.query(AssessmentBlueprint).filter(AssessmentBlueprint.profile_id == pid).delete(
            synchronize_session=False
        )

        # -- resumes: must follow placement_paths (nullable FK into resumes) and the resume-
        # owned leaves above. File deletion is best-effort and never blocks the DB delete. --
        resumes = db.query(Resume).filter(Resume.profile_id == pid).all()
        for resume in resumes:
            try:
                storage.delete(resume.file_path)
            except Exception as exc:
                logger.warning(
                    "Could not delete resume file %s for resume_id=%s during account deletion: %s",
                    resume.file_path,
                    resume.id,
                    exc,
                )
        db.query(Resume).filter(Resume.profile_id == pid).delete(synchronize_session=False)

        # -- job_descriptions: must follow every one of requirements, job_preparation_plans,
        # job_matches, assessment_blueprints, interviews, and roadmaps (all deleted above). -
        db.query(JobDescription).filter(JobDescription.profile_id == pid).delete(synchronize_session=False)

        # -- placement_alarm_challenges: follows its own question-attempt leaf above. -------
        db.query(PlacementAlarmChallenge).filter(PlacementAlarmChallenge.profile_id == pid).delete(
            synchronize_session=False
        )

        # -- Standalone profile-owned tables: no incoming or outgoing FKs to anything else
        # deleted in this function. --------------------------------------------------------
        db.query(Subscription).filter(Subscription.profile_id == pid).delete(synchronize_session=False)
        db.query(Education).filter(Education.profile_id == pid).delete(synchronize_session=False)
        db.query(Certification).filter(Certification.profile_id == pid).delete(synchronize_session=False)
        db.query(Experience).filter(Experience.profile_id == pid).delete(synchronize_session=False)
        db.query(Skill).filter(Skill.profile_id == pid).delete(synchronize_session=False)
        db.query(Project).filter(Project.profile_id == pid).delete(synchronize_session=False)

        db.delete(profile)

    # PasswordResetToken is keyed to the user directly (not the profile), and issued during
    # login/signup before onboarding ever creates a StudentProfile row -- so this must run
    # unconditionally, not nested inside `if profile is not None`.
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.id).delete(synchronize_session=False)

    db.delete(user)
    db.commit()
