import logging

from sqlalchemy.orm import Session

from app.models.arena_attempt import ArenaAttempt
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.career_path_phase import CareerPathPhase
from app.models.career_path_project import CareerPathProject
from app.models.career_path_skill_item import CareerPathSkillItem
from app.models.certification import Certification
from app.models.education import Education
from app.models.evidence_state_snapshot import EvidenceStateSnapshot
from app.models.experience import Experience
from app.models.external_evidence_item import ExternalEvidenceItem
from app.models.external_profile_snapshot import ExternalProfileSnapshot
from app.models.interview import Interview
from app.models.interview_answer import InterviewAnswer
from app.models.interview_evaluation import InterviewEvaluation
from app.models.interview_question import InterviewQuestion
from app.models.job_alert import JobAlert, JobAlertDelivery
from app.models.job_description import JobDescription
from app.models.job_hunt_usage import JobHuntUsage
from app.models.job_listing_match import JobListingMatch
from app.models.job_match import JobMatch
from app.models.job_preparation_plan import JobPreparationPlan
from app.models.job_saved_search import JobSavedSearch
from app.models.job_search_profile import JobSearchProfile
from app.models.mission import Mission
from app.models.notification_event import NotificationEvent
from app.models.password_reset_token import PasswordResetToken
from app.models.placement_alarm_challenge import PlacementAlarmChallenge
from app.models.placement_alarm_question_attempt import PlacementAlarmQuestionAttempt
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.placement_strategy_snapshot import PlacementStrategySnapshot
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.project_build import ProjectBuild
from app.models.project_build_stage import ProjectBuildStage
from app.models.provider_connection import ProviderConnection
from app.models.provider_oauth_state import ProviderOAuthState
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
from app.models.saved_job import SavedJob
from app.models.skill import Skill
from app.models.skill_gap import SkillGap
from app.models.subscription import Subscription
from app.models.user import User
from app.models.weakness_record import WeaknessRecord
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
        # arena_attempts.assessment_id is NOT NULL+unique and missions.assessment_id is
        # nullable but frequently set on completion (mission_service.complete) -- both must
        # go before the `assessments` delete below or that delete raises IntegrityError the
        # moment either feature has ever been used (no FK in this schema has ondelete=CASCADE).
        db.query(ArenaAttempt).filter(ArenaAttempt.profile_id == pid).delete(synchronize_session=False)
        db.query(Mission).filter(Mission.profile_id == pid).delete(synchronize_session=False)
        # Pure leaves: profile_id-owned, referenced by nothing else in this schema.
        db.query(EvidenceStateSnapshot).filter(EvidenceStateSnapshot.profile_id == pid).delete(
            synchronize_session=False
        )
        # Job Hunt's profile-owned rows. All reference job_listings, which is deliberately NOT
        # deleted here: a listing is public provider content shared across every student who
        # searched, so removing it would delete another user's saved job. Nothing personal
        # lives on that table -- the personal part is exactly these rows.
        #
        # Order inside this block is a real foreign-key order, not alphabetical:
        #   job_alert_deliveries -> job_alerts       (deliveries reference the alert)
        #   job_alerts           -> job_saved_searches (an alert may reference a search)
        #   saved_jobs           -> job_saved_searches (provenance FK added in a890ff066a0a)
        # so job_saved_searches has to be last of the four.
        db.query(JobListingMatch).filter(JobListingMatch.profile_id == pid).delete(
            synchronize_session=False
        )
        db.query(JobAlertDelivery).filter(JobAlertDelivery.profile_id == pid).delete(
            synchronize_session=False
        )
        db.query(JobAlert).filter(JobAlert.profile_id == pid).delete(synchronize_session=False)
        db.query(SavedJob).filter(SavedJob.profile_id == pid).delete(synchronize_session=False)
        db.query(JobSavedSearch).filter(JobSavedSearch.profile_id == pid).delete(
            synchronize_session=False
        )
        # The Job Hunt entitlement ledger. A pure leaf -- it references student_profiles and
        # nothing references it -- so its position here is only about being before the profile
        # row itself. Deleting it is correct and is not a way around the one free search: the
        # only route to this code is the owner erasing their whole account, which takes their
        # resumes, interviews and tracker with it. (What it does mean is that the free search
        # is per account, so a new signup gets a new one; closing that would need identity
        # beyond an email address, and is flagged in the report rather than assumed.)
        db.query(JobHuntUsage).filter(JobHuntUsage.profile_id == pid).delete(
            synchronize_session=False
        )
        db.query(NotificationEvent).filter(NotificationEvent.profile_id == pid).delete(synchronize_session=False)
        db.query(ReadinessSnapshot).filter(ReadinessSnapshot.profile_id == pid).delete(synchronize_session=False)
        db.query(WeaknessRecord).filter(WeaknessRecord.profile_id == pid).delete(synchronize_session=False)
        # Career Strategy. Both reference student_profiles, so both must go before the profile
        # row below or the delete fails outright under real foreign keys -- and a student who
        # had merely opened the Career Strategy screen once would then be unable to delete
        # their own account.
        db.query(PlacementStrategySnapshot).filter(
            PlacementStrategySnapshot.profile_id == pid
        ).delete(synchronize_session=False)
        # Professional Presence. The two child tables are owned through provider_connections
        # rather than by profile_id directly, so they cannot be deleted by the profile filter
        # used above -- they need the same subquery shape as career_path_phases and
        # placement_path_topics, and they must both go before provider_connections itself.
        #
        # This block matters more than most for privacy rather than just referential integrity.
        # It is what makes "delete my account" actually destroy the encrypted GitHub access
        # token, everything observed from the student's public repositories, and every snapshot
        # of their external profile. Leaving any of it behind would keep a live third-party
        # credential belonging to someone who no longer has an account -- a credential nobody
        # can reach to revoke, because the only route to it was the account that just went
        # away. provider_oauth_states is included for the same reason even though its rows
        # expire on their own: an in-flight authorisation at the moment of deletion would
        # otherwise outlive the profile it is bound to.
        db.query(ExternalEvidenceItem).filter(
            ExternalEvidenceItem.connection_id.in_(
                db.query(ProviderConnection.id).filter(ProviderConnection.profile_id == pid)
            )
        ).delete(synchronize_session=False)
        db.query(ExternalProfileSnapshot).filter(
            ExternalProfileSnapshot.connection_id.in_(
                db.query(ProviderConnection.id).filter(ProviderConnection.profile_id == pid)
            )
        ).delete(synchronize_session=False)
        db.query(ProviderConnection).filter(ProviderConnection.profile_id == pid).delete(
            synchronize_session=False
        )
        db.query(ProviderOAuthState).filter(ProviderOAuthState.profile_id == pid).delete(
            synchronize_session=False
        )
        # recruiter_attack_turns.session_id and project_build_stages.build_id must go before
        # their respective parents (recruiter_attack_sessions, project_builds) below.
        db.query(RecruiterAttackTurn).filter(
            RecruiterAttackTurn.session_id.in_(
                db.query(RecruiterAttackSession.id).filter(RecruiterAttackSession.profile_id == pid)
            )
        ).delete(synchronize_session=False)
        db.query(ProjectBuildStage).filter(
            ProjectBuildStage.build_id.in_(db.query(ProjectBuild.id).filter(ProjectBuild.profile_id == pid))
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

        # -- recruiter_attack_sessions: its own turns are gone (leaves, above); must precede
        # project_builds below since source_project_build_id nullably references it. ---------
        db.query(RecruiterAttackSession).filter(RecruiterAttackSession.profile_id == pid).delete(
            synchronize_session=False
        )
        # -- project_builds: its own stages are gone and recruiter_attack_sessions no longer
        # references it (both above). -------------------------------------------------------
        db.query(ProjectBuild).filter(ProjectBuild.profile_id == pid).delete(synchronize_session=False)

        # -- assessment_blueprints: must follow everything that references it
        # (placement_path_topics, career_path_skill_items, assessments -- all deleted above)
        # and precede job_descriptions (which it nullably references). ---------------------
        db.query(AssessmentBlueprint).filter(AssessmentBlueprint.profile_id == pid).delete(
            synchronize_session=False
        )

        # -- job_search_profiles: nullably references BOTH resumes and roles, so it has to go
        # before the resumes delete below for the same reason placement_paths does. Its own
        # dependants (job_listing_matches, saved_jobs, job_saved_searches) are already gone
        # above -- though none of them actually reference this table, they are all keyed to
        # the profile directly. ---------------------------------------------------------------
        db.query(JobSearchProfile).filter(JobSearchProfile.profile_id == pid).delete(
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
