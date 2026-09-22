"""Phase 2D -- the orchestration itself.

What is worth testing here is not arithmetic but *what the app is willing to say about a
person*. A job asks for eight things; this app has seen evidence of two of them. The honest
report of that is "evidence found for two, limited for one, nothing on file for five" -- not
"25% match", and above all not "the candidate does not have the other five".

Every assertion below is some form of that one rule. The fixtures build real evidence through
the real models, because the classification being checked is evidence_engine's and mocking it
would test nothing but the mock.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.provider import AIGenerationError, AIProvider
from app.ai.schemas.jd import AIParsedJD, JDRequirementItem
from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_goal import CareerGoal
from app.models.interview import Interview
from app.models.job_description import JobDescription
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.requirement import Requirement
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.weakness_record import WeaknessRecord
from app.schemas.career_goal import CareerGoalCreate, CareerGoalUpdate
from app.services import career_goal_service as svc

_UTC = datetime.timezone.utc

JOB_DESCRIPTION = "Data Engineer. Required: SQL, Python, Azure Data Factory. Preferred: Spark."

PARSED_JD = AIParsedJD(
    role_title="Data Engineer",
    required_skills=[
        JDRequirementItem(value="SQL", is_explicit=True),
        JDRequirementItem(value="Python", is_explicit=True),
        JDRequirementItem(value="Azure Data Factory", is_explicit=True),
    ],
    preferred_skills=[JDRequirementItem(value="Spark", is_explicit=False)],
    responsibilities=["Own the ingestion pipeline"],
    tools_technologies=["Airflow"],
    experience_expectation="2+ years",
    education_eligibility="Bachelor's degree",
    other_signals=[],
    parsing_confidence=0.9,
)


class FakeAI(AIProvider):
    def __init__(self, jd=None, fail=False):
        self._jd = jd or PARSED_JD
        self._fail = fail
        self.calls = 0

    def generate_structured(self, prompt, schema):
        if schema is AIParsedJD:
            self.calls += 1
            if self._fail:
                raise AIGenerationError("provider unavailable")
            return self._jd
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

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


@pytest.fixture()
def profile(db):
    profile = StudentProfile(user_id=1)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _goal(db, profile, **overrides):
    fields = {
        "title": "Data Engineer",
        "company": "Microsoft",
        "location": "Dubai",
        "job_description": JOB_DESCRIPTION,
    }
    fields.update(overrides)
    return svc.create(db, profile.id, CareerGoalCreate(**fields))


def _analyzed_goal(db, profile, ai=None):
    goal = _goal(db, profile)
    svc.analyze(db, profile.id, goal.id, ai or FakeAI())
    return goal


# ------------------------------------------------------------------------------------------
# Evidence builders -- real rows through the real models, so evidence_engine does the judging
# ------------------------------------------------------------------------------------------


def _resume_claim(db, profile_id, skills):
    """A resume keyword: the student told us, nothing confirmed it. self_asserted evidence."""
    resume = Resume(profile_id=profile_id, file_path="x", raw_text="...", parse_status="completed")
    db.add(resume)
    db.commit()
    db.refresh(resume)
    db.add(
        ResumeAnalysis(
            resume_id=resume.id, extracted_skills=skills, extracted_education=[],
            extracted_experience=[], extracted_projects=[], extracted_certifications=[],
            ats_score=80.0, weak_sections=[], extraction_confidence=0.9,
        )
    )
    db.commit()


def _scored_assessment(db, profile_id, skill_scores, priority_weaknesses=None):
    blueprint = AssessmentBlueprint(
        role_id=None, jd_id=None, profile_id=profile_id, source="default_taxonomy", sections=[]
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(
        AssessmentResult(
            assessment_id=assessment.id, skill_scores=skill_scores, overall_score=0.9,
            # What weakness_service actually reads to decide a skill is weak -- the grader's
            # own list, not the raw scores.
            priority_weaknesses=priority_weaknesses or [],
            created_at=datetime.datetime.now(_UTC),
        )
    )
    db.commit()


def _project(db, profile_id, title, description):
    db.add(Project(profile_id=profile_id, title=title, description=description))
    db.commit()


def _completed_interview(db, profile_id, when=None):
    interview = Interview(
        profile_id=profile_id, type="technical", mode="text", status="completed",
        created_at=when or datetime.datetime.now(_UTC).replace(tzinfo=None),
    )
    db.add(interview)
    db.commit()
    db.refresh(interview)
    return interview


# ==========================================================================================
# The three-way answer
# ==========================================================================================


def test_a_skill_with_no_evidence_is_unknown_not_a_missing_skill(db, profile):
    """The single most important behaviour in this phase.

    Nothing in this profile mentions any of the job's skills. The honest report is that nothing
    was found -- an absence in our records. It must never be phrased, bucketed or scored as a
    statement that the candidate lacks the skill.
    """
    goal = _analyzed_goal(db, profile)

    detail = svc.get_detail(db, profile, goal.id)

    assert {item.skill for item in detail.unknown} == {"SQL", "Python", "Azure Data Factory", "Spark", "Airflow"}
    assert detail.aligned == []
    assert detail.needs_attention == []
    for item in detail.unknown:
        assert item.verdict == "no_evidence"
        assert item.verdict_label == "No evidence found"
        assert item.evidence_count == 0


def test_a_resume_only_claim_is_limited_evidence_not_alignment(db, profile):
    """The CLAIMED case from the product spec: Azure appears on the resume and nowhere else.

    It is real -- the student did say it -- so it is not "no evidence". It is also not something
    they could defend under questioning, so it is not alignment either.
    """
    _resume_claim(db, profile.id, ["Azure Data Factory"])
    goal = _analyzed_goal(db, profile)

    detail = svc.get_detail(db, profile, goal.id)

    adf = next(item for item in detail.needs_attention if item.skill == "Azure Data Factory")
    assert adf.verdict == "limited_evidence"
    assert adf.evidence_state == "supported"
    assert adf.sources == ["resume"]
    assert adf.last_verified_at is None, "a claim is not a verification"
    assert all(item.skill != "Azure Data Factory" for item in detail.aligned)


def test_several_independent_sources_become_alignment(db, profile):
    """The SUPPORTED case from the spec: a scored assessment plus a real project. Two
    independent sources, at least one of them not self-asserted, is evidence_engine's own bar
    for `strong` -- this phase does not invent a second one."""
    _scored_assessment(db, profile.id, {"SQL": 0.91})
    _project(db, profile.id, "Warehouse loader", "Built an ingestion pipeline in SQL")
    goal = _analyzed_goal(db, profile)

    detail = svc.get_detail(db, profile, goal.id)

    sql = next(item for item in detail.aligned if item.skill == "SQL")
    assert sql.verdict == "evidence_found"
    assert sql.verdict_label == "Evidence found"
    assert set(sql.sources) == {"assessment", "project"}
    assert sql.last_verified_at is not None


def test_the_verdict_never_states_anything_about_the_candidate(db, profile):
    """Wording is part of the contract here. Every label this endpoint can emit reports what
    was found; none of them judges the person."""
    _resume_claim(db, profile.id, ["Python"])
    _scored_assessment(db, profile.id, {"SQL": 0.91})
    _project(db, profile.id, "Loader", "SQL pipeline")
    goal = _analyzed_goal(db, profile)

    detail = svc.get_detail(db, profile, goal.id)

    labels = {item.verdict_label.lower() for group in (detail.aligned, detail.needs_attention, detail.unknown) for item in group}
    assert labels, "the fixture should have produced some skills"
    for label in labels:
        for forbidden in ("qualified", "unqualified", "not a fit", "you lack", "does not have", "unsuitable"):
            assert forbidden not in label


def test_a_skill_named_twice_by_the_posting_is_reported_once(db, profile):
    """A JD can name the same technology as both a skill and a tool. Two identical rows in the
    UI would read as two separate gaps."""
    parsed = PARSED_JD.model_copy(update={"tools_technologies": ["SQL"]})
    goal = _analyzed_goal(db, profile, ai=FakeAI(jd=parsed))

    detail = svc.get_detail(db, profile, goal.id)

    skills = [item.skill for group in (detail.aligned, detail.needs_attention, detail.unknown) for item in group]
    assert skills.count("SQL") == 1


def test_responsibilities_and_education_are_shown_but_never_judged_as_gaps(db, profile):
    """There is nothing in the Evidence Engine that "Own the ingestion pipeline" could be
    looked up against, so it is reported as something the posting asks for and is never
    silently counted as a skill the student is missing."""
    goal = _analyzed_goal(db, profile)

    detail = svc.get_detail(db, profile, goal.id)

    categories = {r.category for r in detail.requirements}
    assert {"responsibility", "education", "experience"} <= categories
    judged = {item.skill for group in (detail.aligned, detail.needs_attention, detail.unknown) for item in group}
    assert "Own the ingestion pipeline" not in judged
    assert "Bachelor's degree" not in judged


def test_the_buckets_are_ordered_stably(db, profile):
    """Two reads of an unchanged goal must present the same facts in the same order."""
    goal = _analyzed_goal(db, profile)

    first = svc.get_detail(db, profile, goal.id)
    second = svc.get_detail(db, profile, goal.id)

    assert [i.skill for i in first.unknown] == [i.skill for i in second.unknown]
    # Required before preferred: Spark is the only preferred skill and must come last.
    assert first.unknown[-1].skill == "Spark"


# ==========================================================================================
# Progress -- deterministic, and NULL rather than zero
# ==========================================================================================


def test_an_unanalyzed_goal_reports_no_progress_rather_than_zero_percent(db, profile):
    goal = _goal(db, profile)

    detail = svc.get_detail(db, profile, goal.id)

    assert detail.progress.evidence_coverage_percent is None
    assert detail.progress.skills_total == 0
    assert detail.progress.state == "not_analyzed"
    assert detail.is_analyzed is False


def test_coverage_is_a_real_fraction_of_the_jobs_skills(db, profile):
    """Five matchable skills, one of them backed by two independent sources -> 20%. Counted,
    not estimated, and the two inputs are returned alongside it so the number is checkable."""
    _scored_assessment(db, profile.id, {"SQL": 0.91})
    _project(db, profile.id, "Loader", "SQL pipeline")
    goal = _analyzed_goal(db, profile)

    progress = svc.get_detail(db, profile, goal.id).progress

    assert progress.skills_total == 5
    assert progress.skills_with_evidence == 1
    assert progress.evidence_coverage_percent == 20


def test_zero_coverage_on_an_analyzed_goal_is_a_real_zero(db, profile):
    """The distinction that makes the NULL above meaningful: analyzed and found nothing IS a
    measurement, and 0 is the right answer for it."""
    goal = _analyzed_goal(db, profile)

    progress = svc.get_detail(db, profile, goal.id).progress

    assert progress.skills_total == 5
    assert progress.evidence_coverage_percent == 0


def test_a_job_with_no_matchable_skills_reports_null_not_zero(db, profile):
    """A percentage computed over an empty set is not a low score -- it is not a score."""
    parsed = PARSED_JD.model_copy(
        update={"required_skills": [], "preferred_skills": [], "tools_technologies": []}
    )
    goal = _analyzed_goal(db, profile, ai=FakeAI(jd=parsed))

    progress = svc.get_detail(db, profile, goal.id).progress

    assert progress.skills_total == 0
    assert progress.evidence_coverage_percent is None


def test_only_interviews_completed_after_the_goal_was_created_count(db, profile):
    """Preparation means what the student did about THIS job. An interview from before the goal
    existed was not preparation for it."""
    _completed_interview(db, profile.id, when=datetime.datetime(2020, 1, 1))
    goal = _analyzed_goal(db, profile)

    assert svc.get_detail(db, profile, goal.id).progress.interviews_since_created == 0

    _completed_interview(db, profile.id)

    assert svc.get_detail(db, profile, goal.id).progress.interviews_since_created == 1


def test_progress_is_stable_across_repeated_reads(db, profile):
    _scored_assessment(db, profile.id, {"SQL": 0.91})
    _project(db, profile.id, "Loader", "SQL pipeline")
    goal = _analyzed_goal(db, profile)

    readings = [svc.get_detail(db, profile, goal.id).progress.model_dump() for _ in range(3)]

    assert readings[0] == readings[1] == readings[2]


def test_the_state_machine_moves_only_on_real_signals(db, profile):
    goal = _goal(db, profile)
    assert svc.get_detail(db, profile, goal.id).progress.state == "not_analyzed"

    svc.analyze(db, profile.id, goal.id, FakeAI())
    assert svc.get_detail(db, profile, goal.id).progress.state == "not_started"

    _completed_interview(db, profile.id)
    assert svc.get_detail(db, profile, goal.id).progress.state == "in_progress"


def test_ready_for_review_needs_coverage_and_no_open_risks(db, profile):
    """The one state that sounds like an endorsement, so its inputs are pinned down. Note it
    says the preparation looks complete enough to go over -- never that the student will pass."""
    for skill in ("SQL", "Python", "Azure Data Factory", "Spark", "Airflow"):
        _scored_assessment(db, profile.id, {skill: 0.95})
        _project(db, profile.id, f"{skill} project", f"Built something with {skill}")
    goal = _analyzed_goal(db, profile)
    _completed_interview(db, profile.id)

    detail = svc.get_detail(db, profile, goal.id)

    assert detail.progress.evidence_coverage_percent == 100
    assert detail.progress.open_risks == 0
    assert detail.progress.state == "ready_for_review"


# ==========================================================================================
# Interview risk -- from recorded signals only
# ==========================================================================================


def test_no_recorded_signals_means_no_risks_rather_than_invented_ones(db, profile):
    goal = _analyzed_goal(db, profile)

    assert svc.get_detail(db, profile, goal.id).interview_risks == []


def test_a_measured_assessment_weakness_becomes_a_preparation_area(db, profile):
    _scored_assessment(db, profile.id, {"Python": 0.2}, priority_weaknesses=["Python"])
    goal = _analyzed_goal(db, profile)

    risks = svc.get_detail(db, profile, goal.id).interview_risks

    assert [risk.skill_tag for risk in risks] == ["Python"]
    assert risks[0].severity in ("medium", "high")
    # weakness_service's own source vocabulary for a graded test result.
    assert risks[0].sources == ["test_result"]


def test_interview_memory_is_attached_where_a_record_exists(db, profile):
    """The memory loop closing: a dimension this student has actually been graded weak on
    carries its real occurrence count and status from Interview Memory, rather than Phase 2D
    keeping a second copy of the same thing."""
    db.add(
        WeaknessRecord(
            profile_id=profile.id, skill_key="interview:reasoning", status="REPEATED",
            occurrences=3, consecutive_good_signals=0, last_score=0.3,
            last_seen_at=datetime.datetime.now(_UTC),
        )
    )
    _scored_assessment(db, profile.id, {"reasoning": 0.2}, priority_weaknesses=["reasoning"])
    db.commit()
    goal = _analyzed_goal(db, profile)

    risks = svc.get_detail(db, profile, goal.id).interview_risks

    reasoning = next(risk for risk in risks if risk.skill_tag == "reasoning")
    assert reasoning.occurrences == 3
    assert reasoning.memory_status == "REPEATED"
    assert reasoning.label == "Reasoning"


def test_a_risk_with_no_memory_record_reports_null_rather_than_zero(db, profile):
    """A dimension nobody has been graded on has no occurrence count. Zero would claim it had
    been looked at and found clean."""
    _scored_assessment(db, profile.id, {"Python": 0.2}, priority_weaknesses=["Python"])
    goal = _analyzed_goal(db, profile)

    risk = svc.get_detail(db, profile, goal.id).interview_risks[0]

    assert risk.occurrences is None
    assert risk.memory_status is None


# ==========================================================================================
# Preparation plan -- a mapping onto existing features
# ==========================================================================================


def test_an_unanalyzed_goal_is_told_to_analyze_and_nothing_else(db, profile):
    goal = _goal(db, profile)

    plan = svc.get_detail(db, profile, goal.id).preparation_plan

    assert [item.key for item in plan] == ["analyze"]


def test_the_plan_points_only_at_features_that_already_exist(db, profile):
    """Phase 2D creates no new destination. Every key here is one career_strategy already
    emits, plus the two interview modes this app already ships -- and the Android side resolves
    all of them through one table."""
    _resume_claim(db, profile.id, ["Python"])
    _scored_assessment(db, profile.id, {"SQL": 0.2}, priority_weaknesses=["SQL"])
    goal = _analyzed_goal(db, profile)

    plan = svc.get_detail(db, profile, goal.id).preparation_plan

    allowed = {
        "career_goal_analyze", "project_builder", "practice", "recruiter_attack",
        "full_simulation", "skill_passport", "interview", "resume_ats",
    }
    assert plan, "an analyzed goal with gaps should have a plan"
    for item in plan:
        assert item.destination in allowed, f"unknown destination {item.destination!r}"


def test_a_missing_skill_produces_a_build_evidence_step(db, profile):
    goal = _analyzed_goal(db, profile)

    plan = svc.get_detail(db, profile, goal.id).preparation_plan

    build = next(item for item in plan if item.key == "build_evidence")
    assert build.skill in {"SQL", "Python", "Azure Data Factory", "Spark", "Airflow"}
    assert build.destination == "project_builder"


def test_a_goal_with_nothing_outstanding_still_gets_a_step(db, profile):
    """An empty plan would render as a blank panel. "Nothing outstanding" is a real state and
    deserves to say so."""
    for skill in ("SQL", "Python", "Azure Data Factory", "Spark", "Airflow"):
        _scored_assessment(db, profile.id, {skill: 0.95})
        _project(db, profile.id, f"{skill} project", f"Built something with {skill}")
    goal = _analyzed_goal(db, profile)
    _completed_interview(db, profile.id)

    plan = svc.get_detail(db, profile, goal.id).preparation_plan

    assert [item.key for item in plan] == ["keep_sharp"]


def test_the_existing_next_best_action_is_reused_not_reimplemented(db, profile):
    """Phase 2D adds no second recommendation engine. The field is populated by
    next_best_action_service, and its destination vocabulary is that engine's own."""
    goal = _analyzed_goal(db, profile)

    detail = svc.get_detail(db, profile, goal.id)

    assert detail.next_best_action is not None
    assert detail.next_best_action.title


# ==========================================================================================
# Cost and ownership
# ==========================================================================================


def test_reading_the_detail_never_calls_the_ai(db, profile):
    """The reason the detail view is safe to open repeatedly, and the reason this feature costs
    nothing to run."""
    ai = FakeAI()
    goal = _analyzed_goal(db, profile, ai=ai)
    calls_after_analysis = ai.calls

    for _ in range(5):
        svc.get_detail(db, profile, goal.id)

    assert ai.calls == calls_after_analysis == 1


def test_another_profiles_goal_is_not_reachable(db, profile):
    other = StudentProfile(user_id=2)
    db.add(other)
    db.commit()
    db.refresh(other)
    goal = _goal(db, profile)

    for call in (
        lambda: svc.get_detail(db, other, goal.id),
        lambda: svc.update(db, other.id, goal.id, CareerGoalUpdate(company="Hijacked")),
        lambda: svc.delete(db, other.id, goal.id),
        lambda: svc.analyze(db, other.id, goal.id, FakeAI()),
    ):
        with pytest.raises(svc.CareerGoalNotFoundError):
            call()


def test_evidence_from_another_profile_is_never_counted(db, profile):
    """Alignment must come from this student's own evidence only."""
    other = StudentProfile(user_id=2)
    db.add(other)
    db.commit()
    db.refresh(other)
    _scored_assessment(db, other.id, {"SQL": 0.95})
    _project(db, other.id, "Loader", "SQL pipeline")
    goal = _analyzed_goal(db, profile)

    detail = svc.get_detail(db, profile, goal.id)

    assert detail.aligned == []
    assert any(item.skill == "SQL" for item in detail.unknown)


def test_the_goal_cap_is_enforced(db, profile):
    for index in range(svc.MAX_GOALS_PER_PROFILE):
        _goal(db, profile, title=f"Goal {index}")

    with pytest.raises(svc.TooManyCareerGoalsError):
        _goal(db, profile, title="One too many")


# ==========================================================================================
# Analysis lifecycle
# ==========================================================================================


def test_an_ai_failure_leaves_no_partial_parse_behind(db, profile):
    goal = _goal(db, profile)

    with pytest.raises(AIGenerationError):
        svc.analyze(db, profile.id, goal.id, FakeAI(fail=True))

    db.expire_all()
    assert db.get(CareerGoal, goal.id).jd_id is None
    assert db.query(JobDescription).count() == 0
    assert db.query(Requirement).count() == 0


def test_re_analysing_leaves_exactly_one_parse(db, profile):
    goal = _goal(db, profile)

    svc.analyze(db, profile.id, goal.id, FakeAI())
    svc.analyze(db, profile.id, goal.id, FakeAI())

    assert db.query(JobDescription).count() == 1
    assert db.query(Requirement).count() == 8


def test_a_concurrent_second_analysis_does_not_orphan_a_parse(db, profile):
    """Two taps on Analyze. Both parse -- nothing can know the outcome before paying for it --
    but only one may attach, and the other must delete its own work rather than leaving a
    JobDescription and its Requirements behind with nothing pointing at them.

    The second session loads the goal BEFORE the first one commits, which is exactly the state
    a concurrent request is in: its view of jd_id is the pre-analysis one, and the
    compare-and-swap is what notices.
    """
    goal_id = _goal(db, profile).id

    loser_session = sessionmaker(bind=db.get_bind())()
    try:
        # The losing request's read of the goal happens first and sees no analysis. The
        # reference is held for the rest of the test on purpose: SQLAlchemy's identity map is
        # weakly referenced, so dropping it would let the object be collected and the service's
        # own `session.get` would then re-read the winner's committed row -- which is a
        # perfectly valid *sequential* re-analysis, and not the race being tested. A real
        # request holds its loaded goal for its whole duration, and so does this.
        stale_view = loser_session.get(CareerGoal, goal_id)
        assert stale_view.jd_id is None

        svc.analyze(db, profile.id, goal_id, FakeAI())
        winner_jd_id = db.get(CareerGoal, goal_id).jd_id
        assert winner_jd_id is not None

        svc.analyze(loser_session, profile.id, goal_id, FakeAI())
    finally:
        loser_session.close()

    assert stale_view is not None  # keeps the stale read alive for the whole race
    db.expire_all()
    assert db.get(CareerGoal, goal_id).jd_id == winner_jd_id, "the winner's parse must stand"
    assert db.query(JobDescription).count() == 1, "the loser's parse must have been cleaned up"
    assert db.query(Requirement).count() == 8
