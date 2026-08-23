import logging

from sqlalchemy.orm import Session

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
from app.models.question_attempt import QuestionAttempt
from app.models.requirement import Requirement
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.roadmap import Roadmap
from app.models.roadmap_task import RoadmapTask
from app.models.skill import Skill
from app.models.skill_gap import SkillGap
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
    remove another user's data. Deletes are ordered children-before-parents to satisfy FK
    constraints on a real (constraint-enforcing) database, not just SQLite's default
    unenforced FKs.
    """
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()

    if profile is not None:
        resumes = db.query(Resume).filter(Resume.profile_id == profile.id).all()
        resume_ids = [r.id for r in resumes]
        if resume_ids:
            db.query(ResumeAnalysis).filter(ResumeAnalysis.resume_id.in_(resume_ids)).delete(
                synchronize_session=False
            )
        for resume in resumes:
            try:
                storage.delete(resume.file_path)
            except Exception as exc:  # best-effort: a stuck file must never block account deletion
                logger.warning(
                    "Could not delete resume file %s for resume_id=%s during account deletion: %s",
                    resume.file_path,
                    resume.id,
                    exc,
                )
        db.query(Resume).filter(Resume.profile_id == profile.id).delete(synchronize_session=False)

        assessment_ids = [
            a.id for a in db.query(Assessment.id).filter(Assessment.profile_id == profile.id).all()
        ]
        if assessment_ids:
            db.query(AssessmentResult).filter(AssessmentResult.assessment_id.in_(assessment_ids)).delete(
                synchronize_session=False
            )
            db.query(QuestionAttempt).filter(QuestionAttempt.assessment_id.in_(assessment_ids)).delete(
                synchronize_session=False
            )
        db.query(Assessment).filter(Assessment.profile_id == profile.id).delete(synchronize_session=False)
        db.query(AssessmentBlueprint).filter(AssessmentBlueprint.profile_id == profile.id).delete(
            synchronize_session=False
        )

        interview_ids = [
            i.id for i in db.query(Interview.id).filter(Interview.profile_id == profile.id).all()
        ]
        if interview_ids:
            question_ids = [
                q.id
                for q in db.query(InterviewQuestion.id)
                .filter(InterviewQuestion.interview_id.in_(interview_ids))
                .all()
            ]
            if question_ids:
                db.query(InterviewAnswer).filter(
                    InterviewAnswer.interview_question_id.in_(question_ids)
                ).delete(synchronize_session=False)
            db.query(InterviewEvaluation).filter(InterviewEvaluation.interview_id.in_(interview_ids)).delete(
                synchronize_session=False
            )
            db.query(InterviewQuestion).filter(InterviewQuestion.interview_id.in_(interview_ids)).delete(
                synchronize_session=False
            )
        db.query(Interview).filter(Interview.profile_id == profile.id).delete(synchronize_session=False)

        roadmap_ids = [
            r.id for r in db.query(Roadmap.id).filter(Roadmap.profile_id == profile.id).all()
        ]
        if roadmap_ids:
            db.query(RoadmapTask).filter(RoadmapTask.roadmap_id.in_(roadmap_ids)).delete(
                synchronize_session=False
            )
        db.query(Roadmap).filter(Roadmap.profile_id == profile.id).delete(synchronize_session=False)

        job_match_ids = [
            m.id for m in db.query(JobMatch.id).filter(JobMatch.profile_id == profile.id).all()
        ]
        if job_match_ids:
            db.query(SkillGap).filter(SkillGap.job_match_id.in_(job_match_ids)).delete(
                synchronize_session=False
            )
        db.query(JobMatch).filter(JobMatch.profile_id == profile.id).delete(synchronize_session=False)

        jd_ids = [
            j.id for j in db.query(JobDescription.id).filter(JobDescription.profile_id == profile.id).all()
        ]
        if jd_ids:
            db.query(Requirement).filter(Requirement.jd_id.in_(jd_ids)).delete(synchronize_session=False)
        db.query(JobDescription).filter(JobDescription.profile_id == profile.id).delete(
            synchronize_session=False
        )

        db.query(Education).filter(Education.profile_id == profile.id).delete(synchronize_session=False)
        db.query(Certification).filter(Certification.profile_id == profile.id).delete(
            synchronize_session=False
        )
        db.query(Experience).filter(Experience.profile_id == profile.id).delete(synchronize_session=False)
        db.query(Skill).filter(Skill.profile_id == profile.id).delete(synchronize_session=False)
        db.query(Project).filter(Project.profile_id == profile.id).delete(synchronize_session=False)

        db.delete(profile)

    db.delete(user)
    db.commit()
