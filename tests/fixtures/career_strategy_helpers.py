"""Row builders for the Career Strategy Phase 1 tests.

Career Strategy reads a dozen tables, so a test that wants "a student with a target role,
one weak assessment, a resume that does not mention the skill, and a tracked job that does"
otherwise spends forty lines on setup before it can assert anything. These builders write real
rows through the real models -- no mocks, no fakes, no monkeypatched services -- so the tests
exercise the same queries production runs.

Shared rather than duplicated per test file because five test modules need the same setup, and
five drifting copies of "what a completed resume looks like" is exactly how a suite starts
passing against a shape production never produces.
"""

import datetime

from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.career_path import CareerPath
from app.models.certification import Certification
from app.models.education import Education
from app.models.experience import Experience
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_listing import JobListing
from app.models.job_listing_match import JobListingMatch
from app.models.job_saved_search import JobSavedSearch
from app.models.job_search_profile import JobSearchProfile
from app.models.mission import Mission
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.project_build import ProjectBuild
from app.models.recruiter_attack_session import RecruiterAttackSession
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.resume_ats_check import ResumeAtsCheck
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.models.saved_job import SavedJob
from app.models.weakness_record import WeaknessRecord
from app.services.job_hunt import matching

UTC = datetime.timezone.utc

# The instant every test injects as `now`, and the anchor `days_ago` counts back from.
#
# Deliberately derived from the real clock rather than hard-coded to a calendar date. Placement
# Strategy takes an injectable `now` throughout, but one of its inputs does not:
# evidence_engine._compute_state_and_confidence reads datetime.now() directly to decide whether
# a skill has gone `stale`. A frozen NOW of, say, 2026-06-01 would therefore drift out of step
# with evidence_engine as real time passed -- rows written "one day before NOW" would silently
# start reading as months old to the staleness check, and tests asserting fresh evidence would
# begin failing on a date nobody chose. Anchoring here to the real clock keeps the two in
# agreement while still pinning a single instant for the whole test run, which is what the
# determinism assertions actually need: the same `now` injected twice must give the same
# answer, not the same answer in every calendar year.
NOW = datetime.datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


def days_ago(days: int, *, now: datetime.datetime = NOW) -> datetime.datetime:
    """Naive UTC, matching how every created_at in this schema round-trips through SQLite."""
    return (now - datetime.timedelta(days=days)).replace(tzinfo=None)


def make_profile(db, *, user_id: int = 1, target_role_id=None, **fields) -> StudentProfile:
    profile = StudentProfile(user_id=user_id, target_role_id=target_role_id, **fields)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def make_role(db, name: str, skills: dict) -> Role:
    """A Role plus its RoleSkillWeights -- the legacy target-role path that role_context falls
    back to when no Career Path exists."""
    role = Role(name=name, category="Engineering")
    db.add(role)
    db.commit()
    db.refresh(role)
    for skill, weight in skills.items():
        db.add(RoleSkillWeight(role_id=role.id, skill=skill, weight=weight, assessment_type="technical"))
    db.commit()
    return role


def make_career_path(db, profile, *, role_key="sde", name="Software Engineer", core_skills=(), via="catalog"):
    path = CareerPath(
        profile_id=profile.id,
        role_family_key=role_key,
        display_role_name=name,
        is_primary=True,
        generated_via=via,
        role_overview={"core_skills": list(core_skills), "tools": []},
    )
    db.add(path)
    db.commit()
    db.refresh(path)
    return path


def add_assessment_result(
    db, profile, skill_scores: dict, *, priority_weaknesses=(), created_at=None, overall=0.7
) -> AssessmentResult:
    """A full blueprint -> assessment -> result chain. evidence_engine and weakness_service both
    join through all three, and a result without its parents is invisible to them.

    `jd_id` stays None so weakness_service's role-scoped query (jd_id=None) picks it up -- the
    JD-scoped variant belongs to job-specific strategy, which is a later phase.
    """
    blueprint = AssessmentBlueprint(profile_id=profile.id, source="default_taxonomy", sections=[], jd_id=None)
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)

    assessment = Assessment(profile_id=profile.id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)

    result = AssessmentResult(
        assessment_id=assessment.id,
        skill_scores=skill_scores,
        overall_score=overall,
        priority_weaknesses=list(priority_weaknesses),
        created_at=created_at or days_ago(1),
    )
    db.add(result)
    db.commit()
    db.refresh(result)
    return result


def add_resume(db, profile, skills=(), *, parse_status="completed") -> Resume:
    """A resume plus its analysis. skill_matching.latest_resume_skills requires BOTH, and only
    counts a resume whose parse_status is 'completed' -- an uploaded-but-unparsed resume is a
    genuinely different state and some tests need exactly that one."""
    resume = Resume(
        profile_id=profile.id,
        file_path="/tmp/resume.pdf",
        raw_text="resume text",
        parse_status=parse_status,
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    if parse_status == "completed":
        db.add(
            ResumeAnalysis(
                resume_id=resume.id,
                extracted_skills=list(skills),
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


def add_ats_check(db, profile, resume, *, overall=80, skills=80, content=80, experience=80, projects=80):
    check = ResumeAtsCheck(
        resume_id=resume.id,
        profile_id=profile.id,
        overall_score=overall,
        score_category="good" if overall >= 70 else "needs_work",
        parsing_score=overall,
        structure_score=overall,
        content_score=content,
        skills_score=skills,
        experience_score=experience,
        projects_score=projects,
        contact_score=overall,
        readability_score=overall,
        category_breakdown=[],
        section_analysis=[],
        issues=[],
        recommendations=[],
        ai_summary="",
        created_at=days_ago(2),
    )
    db.add(check)
    db.commit()
    db.refresh(check)
    return check


def add_interview_evaluation(db, profile, *, created_at=None, **dimensions) -> InterviewEvaluation:
    """Every required dimension defaults to a comfortably-passing 0.8; a test overrides only the
    one it cares about, so an unrelated dimension can never accidentally be the thing that
    triggers the assertion."""
    interview = Interview(profile_id=profile.id, type="technical", mode="text", status="completed", jd_id=None)
    db.add(interview)
    db.commit()
    db.refresh(interview)

    scores = {
        "technical_correctness": 0.8,
        "relevance": 0.8,
        "completeness": 0.8,
        "reasoning": 0.8,
        "structure": 0.8,
        "clarity": 0.8,
        "role_jd_coverage": 0.8,
        "overall_score": 0.8,
    }
    scores.update(dimensions)

    evaluation = InterviewEvaluation(
        interview_id=interview.id,
        feedback_text="",
        created_at=created_at or days_ago(1),
        **scores,
    )
    db.add(evaluation)
    db.commit()
    db.refresh(evaluation)
    return evaluation


def add_recruiter_attack(db, profile, *, technical_defense=0.8, project_ownership=0.8, specificity=0.8,
                         communication=0.8, overall=0.8) -> RecruiterAttackSession:
    session = RecruiterAttackSession(
        profile_id=profile.id,
        source_type="resume_claim",
        source_label="Built an ETL pipeline",
        status="completed",
        technical_defense=technical_defense,
        project_ownership=project_ownership,
        specificity=specificity,
        communication=communication,
        overall_score=overall,
        completed_at=days_ago(1),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def add_learning_topics(db, profile, entries) -> PlacementPath:
    """entries: [(skill_tag, status), ...] where status is 'completed' or 'not_started'."""
    path = PlacementPath(profile_id=profile.id, status="active")
    db.add(path)
    db.commit()
    db.refresh(path)
    for index, (skill_tag, status) in enumerate(entries):
        db.add(
            PlacementPathTopic(
                path_id=path.id,
                stage="core",
                track_key="track",
                topic_key=f"topic-{index}",
                order_in_track=index,
                title=f"Learn {skill_tag}",
                skill_tag=skill_tag,
                status=status,
            )
        )
    db.commit()
    return path


def add_tracked_job(
    db, profile, *, title="Data Engineer", missing_skills=(), matched_skills=(), score=0.6,
    score_version=None, listing_id_seed=1, components=None, status="saved", applied=False,
    location="Bengaluru", work_mode=None, employment_type=None, salary=None,
    rationale="", unknown_factors=(),
) -> JobListing:
    """A saved (tracked) job plus its authoritative match row.

    `score_version` defaults to the live matching.SCORE_VERSION. A test that wants a stale row
    passes an old version explicitly -- signals.collect must ignore it rather than treat a
    superseded score as current.

    `components` writes JobListingMatch.component_scores, which is where Career Strategy reads
    experience fit and role alignment from. Defaults to {} rather than to plausible-looking
    numbers: a component the match engine could not judge is genuinely absent, and a test that
    wants one present has to say so.

    `applied=True` sets applied_at, which is what the tracker actually counts an application
    by -- deliberately independent of `status`, so a rejected application is still an
    application.

    `salary` is a dict of the JobListing salary columns (min/max/currency/period/is_predicted)
    and defaults to None, meaning the provider published nothing. It is never partly filled in
    by default: a test that wants a salary states the period along with the amount, because
    an amount without a period is exactly the bug the strategy must not be able to produce.

    `rationale` and `unknown_factors` populate the stored JobListingMatch columns that
    job_hunt/matching writes and that the job strategy composes rather than re-deriving.
    """
    listing = JobListing(
        provider="test",
        provider_job_id=f"job-{listing_id_seed}",
        url=f"https://example.invalid/jobs/{listing_id_seed}",
        title=title,
        company="Acme",
        location=location,
        work_mode=work_mode,
        employment_type=employment_type,
        **(salary or {}),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)

    db.add(
        SavedJob(
            profile_id=profile.id,
            listing_id=listing.id,
            status=status,
            source="manual",
            applied_at=days_ago(5) if applied else None,
        )
    )
    db.add(
        JobListingMatch(
            profile_id=profile.id,
            listing_id=listing.id,
            total_score=score,
            score_version=score_version or matching.SCORE_VERSION,
            component_scores=dict(components or {}),
            matched_skills=list(matched_skills),
            missing_skills=list(missing_skills),
            unknown_factors=list(unknown_factors),
            rationale=rationale,
            calculated_at=days_ago(1),
        )
    )
    db.commit()
    return listing


def add_untracked_listing(db, *, title="Platform Engineer", listing_id_seed=900) -> JobListing:
    """A listing with no SavedJob behind it -- the student has never saved or applied to it.

    Listings are shared rows rather than profile-owned, so this is a real and reachable case:
    a student can open any advert. What they must not get is an invented application history
    for one they never touched.
    """
    listing = JobListing(
        provider="test",
        provider_job_id=f"job-{listing_id_seed}",
        url=f"https://example.invalid/jobs/{listing_id_seed}",
        title=title,
        company="Acme",
        location="Bengaluru",
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return listing


def add_weakness_record(db, profile, skill_key, *, status="REPEATED", occurrences=2, last_score=0.4):
    record = WeaknessRecord(
        profile_id=profile.id,
        skill_key=skill_key,
        status=status,
        occurrences=occurrences,
        consecutive_good_signals=0,
        last_score=last_score,
        last_seen_at=days_ago(1),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def add_project(db, profile, *, title="ETL Pipeline", tech_stack="Airflow") -> Project:
    """A free-text profile Project -- the student typed this in, so evidence_engine marks it
    self_asserted. Distinct from a completed Project Builder build, which is real work."""
    project = Project(profile_id=profile.id, title=title, description="", tech_stack=tech_stack)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def add_project_build(db, profile, *, title="SQL Pipeline", stack_summary="SQL") -> ProjectBuild:
    """A *completed* Project Builder build. Unlike the free-text Project row above, the student
    actually did the work and marked it done, so evidence_engine counts it as independent
    (self_asserted=False) and it can move a skill to `strong`. That distinction is the whole
    reason both builders exist."""
    build = ProjectBuild(
        profile_id=profile.id,
        title=title,
        stack_summary=stack_summary,
        status="completed",
        completed_at=days_ago(3),
    )
    db.add(build)
    db.commit()
    db.refresh(build)
    return build


def add_mission(db, profile, *, status="completed", days: int = 1) -> Mission:
    mission = Mission(
        profile_id=profile.id,
        mission_date=(NOW - datetime.timedelta(days=days)).date(),
        title="Practice SQL",
        action_type="practice",
        reasons=[],
        status=status,
    )
    db.add(mission)
    db.commit()
    db.refresh(mission)
    return mission


def add_experience(db, profile, *, company="Acme", title="Data Analyst") -> Experience:
    row = Experience(profile_id=profile.id, company=company, title=title)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_education(db, profile, *, institution="State University", degree="B.Tech") -> Education:
    row = Education(profile_id=profile.id, institution=institution, degree=degree)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_certification(db, profile, *, name="AWS Cloud Practitioner") -> Certification:
    row = Certification(profile_id=profile.id, name=name)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_job_preferences(
    db, profile, *, preferred_companies=(), open_to_relocation=None, experience_level=None,
    normalized_skills=(),
) -> JobSearchProfile:
    """The student's stated job-search preferences.

    `open_to_relocation` stays tri-state: None means they were never asked, which the existing
    match engine treats differently from an explicit "no". Defaulting it to False here would
    bake an answer the student never gave into every test fixture.

    `normalized_skills` defaults to empty for the same reason, and it is load-bearing rather
    than cosmetic: job_hunt/provenance reports an advert's unmatched skills as UNCLEAR rather
    than MISSING while this list is empty, because with no skill list for the student there is
    genuinely no way to tell a gap from an omission. A test that wants real gaps has to say what
    the student's skills are.
    """
    row = JobSearchProfile(
        profile_id=profile.id,
        preferred_companies=list(preferred_companies),
        open_to_relocation=open_to_relocation,
        experience_level=experience_level,
        normalized_skills=list(normalized_skills),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_saved_search(db, profile, *, name="Data Engineer, Bengaluru", filters=None) -> JobSavedSearch:
    row = JobSavedSearch(
        profile_id=profile.id,
        name=name,
        filters=dict(filters or {"q": "data engineer"}),
        last_run_at=days_ago(2),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
