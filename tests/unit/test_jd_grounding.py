"""The deterministic half of the Job Match & Prep plan.

These tests carry most of the feature's anti-fabrication weight. Everything here runs without
an AI provider, because that is the point: what the student is told they have, are missing, or
have built is decided in code, and the model only writes prose about it afterwards.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.requirement import Requirement
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.user import User
from app.services import jd_grounding


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _profile(db, email="ground@example.com"):
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    profile = StudentProfile(user_id=user.id, full_name="Asha Rao")
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _resume_with(db, profile_id, *, skills=None, projects=None, weak_sections=None, experience=None):
    resume = Resume(
        profile_id=profile_id,
        file_path="resumes/x.pdf",
        original_filename="cv.pdf",
        file_size_bytes=100,
        raw_text="resume text",
        parse_status="completed",
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    analysis = ResumeAnalysis(
        resume_id=resume.id,
        extracted_skills=skills or [],
        extracted_education=[],
        extracted_experience=experience or [],
        extracted_projects=projects or [],
        extracted_certifications=[],
        ats_score=70.0,
        weak_sections=weak_sections or [],
        extraction_confidence=0.9,
    )
    db.add(analysis)
    db.commit()
    return resume


def _requirement(jd_id, value, *, is_explicit=True, importance="required", category="skill"):
    return Requirement(
        jd_id=jd_id, category=category, value=value, is_explicit=is_explicit, importance=importance
    )


class TestCollectResumeFacts:
    def test_reports_no_resume_instead_of_raising(self, db_session):
        """The shared latest_resume_skills raises here, which Placement Path and Placement
        Alarm depend on. This flow degrades instead, so it needs its own non-raising reader."""
        profile = _profile(db_session)

        facts = jd_grounding.collect_resume_facts(db_session, profile.id)

        assert facts.has_resume is False
        assert facts.skills == []
        assert facts.is_informative is False

    def test_reads_skills_projects_and_weak_sections(self, db_session):
        profile = _profile(db_session)
        _resume_with(
            db_session,
            profile.id,
            skills=["Python", "SQL"],
            projects=[{"title": "Azure Data Pipeline", "tech_stack": "Azure Data Factory, Python"}],
            weak_sections=["no quantified achievements"],
            experience=[{"company": "Acme", "title": "Intern"}],
        )

        facts = jd_grounding.collect_resume_facts(db_session, profile.id)

        assert facts.has_resume is True
        assert facts.skills == ["Python", "SQL"]
        assert [p.title for p in facts.projects] == ["Azure Data Pipeline"]
        assert facts.weak_sections == ["no quantified achievements"]
        assert facts.experience_count == 1
        assert facts.is_informative is True

    def test_skips_malformed_project_entries(self, db_session):
        """extracted_projects is parsed JSON, so its shape is not guaranteed. An entry without
        a usable title is dropped rather than rendered as an untitled project."""
        profile = _profile(db_session)
        _resume_with(
            db_session,
            profile.id,
            skills=["Python"],
            projects=[
                {"title": "Real Project", "description": "built a thing"},
                {"title": "   "},
                {"description": "no title at all"},
                "not even a dict",
                None,
            ],
        )

        facts = jd_grounding.collect_resume_facts(db_session, profile.id)

        assert [p.title for p in facts.projects] == ["Real Project"]

    def test_includes_profile_projects_alongside_resume_projects(self, db_session):
        profile = _profile(db_session)
        _resume_with(db_session, profile.id, skills=["Python"], projects=[{"title": "Resume Project"}])
        db_session.add(Project(profile_id=profile.id, title="Profile Project", tech_stack="Kafka"))
        db_session.commit()

        facts = jd_grounding.collect_resume_facts(db_session, profile.id)

        assert {p.title: p.source for p in facts.projects} == {
            "Resume Project": "resume",
            "Profile Project": "profile",
        }

    def test_a_parsed_but_empty_resume_is_not_informative(self, db_session):
        """Advice tailored to an empty extraction would be advice tailored to nothing."""
        profile = _profile(db_session)
        _resume_with(db_session, profile.id, skills=[], projects=[])

        facts = jd_grounding.collect_resume_facts(db_session, profile.id)

        assert facts.has_resume is True
        assert facts.is_informative is False


class TestGroundGaps:
    def test_carries_the_jds_own_framing_of_each_requirement(self, db_session):
        requirements = [
            _requirement(1, "Kafka", is_explicit=True, importance="required"),
            _requirement(1, "Airflow", is_explicit=False, importance="preferred"),
        ]

        grounded = jd_grounding.ground_gaps(requirements, {"Kafka": "high", "Airflow": "medium"})

        by_skill = {g.skill: g for g in grounded}
        assert by_skill["Kafka"].is_explicit_requirement is True
        assert by_skill["Kafka"].requirement_importance == "required"
        assert by_skill["Airflow"].is_explicit_requirement is False
        assert by_skill["Airflow"].requirement_importance == "preferred"

    def test_drops_a_gap_with_no_matching_requirement(self, db_session):
        """Without a Requirement row we cannot say how the JD framed the skill, and guessing
        is exactly what this layer exists to prevent."""
        requirements = [_requirement(1, "SQL")]

        grounded = jd_grounding.ground_gaps(requirements, {"SQL": "high", "Blockchain": "high"})

        assert [g.skill for g in grounded] == ["SQL"]

    def test_explicit_required_gaps_come_first(self, db_session):
        requirements = [
            _requirement(1, "Inferred", is_explicit=False, importance="preferred"),
            _requirement(1, "Stated", is_explicit=True, importance="required"),
        ]

        grounded = jd_grounding.ground_gaps(requirements, {"Inferred": "medium", "Stated": "high"})

        assert [g.skill for g in grounded] == ["Stated", "Inferred"]

    def test_caps_how_many_gaps_get_guidance(self, db_session):
        requirements = [_requirement(1, f"Skill{i}") for i in range(30)]
        severities = {f"Skill{i}": "high" for i in range(30)}

        grounded = jd_grounding.ground_gaps(requirements, severities)

        assert len(grounded) == jd_grounding.MAX_GAPS_WITH_GUIDANCE


class TestMissingKeywords:
    def test_lists_jd_terms_absent_from_the_resume(self, db_session):
        facts = jd_grounding.ResumeFacts(has_resume=True, skills=["Python", "SQL"])
        requirements = [_requirement(1, "Python"), _requirement(1, "Kafka"), _requirement(1, "Airflow")]

        assert jd_grounding.missing_keywords(requirements, facts) == ["Kafka", "Airflow"]

    def test_agrees_with_the_matching_engine_about_what_counts_as_present(self, db_session):
        """Uses the same token check as matching_service, so a keyword can never be reported
        missing while the matcher counts it as matched."""
        facts = jd_grounding.ResumeFacts(has_resume=True, skills=["react.js"])
        requirements = [_requirement(1, "React")]

        assert jd_grounding.missing_keywords(requirements, facts) == []

    def test_returns_nothing_when_there_is_no_resume(self, db_session):
        """Absence of a resume is not evidence that a keyword is absent from it."""
        facts = jd_grounding.ResumeFacts(has_resume=False)
        requirements = [_requirement(1, "Kafka")]

        assert jd_grounding.missing_keywords(requirements, facts) == []

    def test_deduplicates_and_caps(self, db_session):
        facts = jd_grounding.ResumeFacts(has_resume=True, skills=[])
        requirements = [_requirement(1, "Kafka"), _requirement(1, "kafka")] + [
            _requirement(1, f"Tool{i}") for i in range(40)
        ]

        keywords = jd_grounding.missing_keywords(requirements, facts)

        assert keywords.count("Kafka") == 1
        assert len(keywords) == jd_grounding.MAX_MISSING_KEYWORDS


class TestMatchProjects:
    def test_connects_a_requirement_to_a_project_that_evidences_it(self, db_session):
        facts = jd_grounding.ResumeFacts(
            has_resume=True,
            skills=["Azure"],
            projects=[
                jd_grounding.ResumeProject(
                    title="Azure Data Pipeline",
                    description="Ingestion pipeline",
                    tech_stack="Azure Data Factory",
                    source="resume",
                )
            ],
        )
        requirements = [_requirement(1, "Azure Data Factory")]

        matches = jd_grounding.match_projects(requirements, facts)

        assert len(matches) == 1
        assert matches[0].project.title == "Azure Data Pipeline"
        assert matches[0].requirement == "Azure Data Factory"

    def test_a_skill_alone_never_produces_a_project_connection(self, db_session):
        """Knowing Kafka is not the same as having built something with it, and presenting the
        second when only the first is true is the fabrication this feature must never commit."""
        facts = jd_grounding.ResumeFacts(
            has_resume=True,
            skills=["Kafka"],
            projects=[
                jd_grounding.ResumeProject(
                    title="Portfolio Website", description="A static site", tech_stack="HTML", source="resume"
                )
            ],
        )
        requirements = [_requirement(1, "Kafka")]

        assert jd_grounding.match_projects(requirements, facts) == []

    def test_no_projects_means_no_connections(self, db_session):
        facts = jd_grounding.ResumeFacts(has_resume=True, skills=["Azure"], projects=[])

        assert jd_grounding.match_projects([_requirement(1, "Azure")], facts) == []

    def test_does_not_repeat_the_same_pairing(self, db_session):
        facts = jd_grounding.ResumeFacts(
            has_resume=True,
            projects=[
                jd_grounding.ResumeProject(
                    title="SQL Dashboard", description="reporting", tech_stack="SQL", source="resume"
                )
            ],
        )
        requirements = [_requirement(1, "SQL"), _requirement(1, "SQL", importance="preferred")]

        assert len(jd_grounding.match_projects(requirements, facts)) == 1


class TestRenderedPromptBlocks:
    def test_no_resume_block_states_the_absence_plainly(self):
        rendered = jd_grounding.render_resume_facts(jd_grounding.ResumeFacts(has_resume=False))

        assert "No resume uploaded" in rendered
        assert "NO information" in rendered

    def test_resume_block_summarises_rather_than_shipping_the_whole_resume(self):
        facts = jd_grounding.ResumeFacts(
            has_resume=True,
            skills=["Python"],
            projects=[
                jd_grounding.ResumeProject(
                    title="Pipeline", description="d", tech_stack="Azure", source="resume"
                )
            ],
            experience_count=2,
        )

        rendered = jd_grounding.render_resume_facts(facts)

        assert "Python" in rendered
        assert "Pipeline" in rendered
        # The raw resume text is personal data the generation step does not need.
        assert "resume text" not in rendered

    def test_gap_block_distinguishes_stated_from_inferred(self):
        gaps = [
            jd_grounding.GroundedGap("Kafka", "high", True, "required", "Kafka"),
            jd_grounding.GroundedGap("Airflow", "medium", False, "preferred", "Airflow"),
        ]

        rendered = jd_grounding.render_gap_context(gaps)

        assert "states this requirement outright" in rendered
        assert "we inferred it" in rendered

    def test_empty_blocks_say_none_rather_than_rendering_nothing(self):
        assert jd_grounding.render_gap_context([]) == "none"
        assert jd_grounding.render_project_context([]) == "none"
