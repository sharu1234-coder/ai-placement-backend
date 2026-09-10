from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from app.core.config import settings
from app.database.base import Base

# Imported for their side effect of registering tables with Base.metadata.
from app.models.arena_attempt import ArenaAttempt  # noqa: F401
from app.models.assessment import Assessment  # noqa: F401
from app.models.assessment_blueprint import AssessmentBlueprint  # noqa: F401
from app.models.assessment_result import AssessmentResult  # noqa: F401
from app.models.career_path import CareerPath  # noqa: F401
from app.models.career_path_phase import CareerPathPhase  # noqa: F401
from app.models.career_path_project import CareerPathProject  # noqa: F401
from app.models.career_path_skill_item import CareerPathSkillItem  # noqa: F401
from app.models.certification import Certification  # noqa: F401
from app.models.education import Education  # noqa: F401
from app.models.evidence_state_snapshot import EvidenceStateSnapshot  # noqa: F401
from app.models.experience import Experience  # noqa: F401
from app.models.interview import Interview  # noqa: F401
from app.models.interview_answer import InterviewAnswer  # noqa: F401
from app.models.interview_evaluation import InterviewEvaluation  # noqa: F401
from app.models.interview_question import InterviewQuestion  # noqa: F401
from app.models.job_description import JobDescription  # noqa: F401
from app.models.job_hunt_usage import JobHuntUsage  # noqa: F401
from app.models.job_alert import JobAlert, JobAlertDelivery  # noqa: F401
from app.models.job_listing import JobListing  # noqa: F401
from app.models.job_listing_match import JobListingMatch  # noqa: F401
from app.models.job_match import JobMatch  # noqa: F401
from app.models.job_preparation_plan import JobPreparationPlan  # noqa: F401
from app.models.job_saved_search import JobSavedSearch  # noqa: F401
from app.models.job_search_profile import JobSearchProfile  # noqa: F401
from app.models.mission import Mission  # noqa: F401
from app.models.notification_event import NotificationEvent  # noqa: F401
from app.models.password_reset_token import PasswordResetToken  # noqa: F401
from app.models.placement_alarm_challenge import PlacementAlarmChallenge  # noqa: F401
from app.models.placement_alarm_question_attempt import PlacementAlarmQuestionAttempt  # noqa: F401
from app.models.placement_path import PlacementPath  # noqa: F401
from app.models.placement_path_topic import PlacementPathTopic  # noqa: F401
from app.models.profile import StudentProfile  # noqa: F401
from app.models.provider_call_budget import ProviderCallBudget  # noqa: F401
from app.models.project import Project  # noqa: F401
from app.models.project_build import ProjectBuild  # noqa: F401
from app.models.project_build_stage import ProjectBuildStage  # noqa: F401
from app.models.question import Question  # noqa: F401
from app.models.question_attempt import QuestionAttempt  # noqa: F401
from app.models.readiness_snapshot import ReadinessSnapshot  # noqa: F401
from app.models.recruiter_attack_session import RecruiterAttackSession  # noqa: F401
from app.models.recruiter_attack_turn import RecruiterAttackTurn  # noqa: F401
from app.models.requirement import Requirement  # noqa: F401
from app.models.resume import Resume  # noqa: F401
from app.models.resume_analysis import ResumeAnalysis  # noqa: F401
from app.models.resume_ats_check import ResumeAtsCheck  # noqa: F401
from app.models.roadmap import Roadmap  # noqa: F401
from app.models.roadmap_task import RoadmapTask  # noqa: F401
from app.models.saved_job import SavedJob  # noqa: F401
from app.models.role import Role  # noqa: F401
from app.models.role_skill_weight import RoleSkillWeight  # noqa: F401
from app.models.skill import Skill  # noqa: F401
from app.models.skill_gap import SkillGap  # noqa: F401
from app.models.subscription import Subscription  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.weakness_record import WeaknessRecord  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
