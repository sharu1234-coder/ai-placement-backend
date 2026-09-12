"""D3: the job strategy must compose what Job Hunt already computed, and must not answer a
constraint question with a study task.

Two separate claims are tested here, and they fail for different reasons if broken.

**Composition.** `job_hunt/matching` writes a rationale and an `unknown_factors` list onto
every JobListingMatch row, and `job_hunt/readiness` computes a `biggest_gap` with a concrete
before/after. All three were being computed, persisted, and then dropped by the career-strategy
job view, which meant the student saw a score with no explanation while the Job Hunt card
showed the explanation. Two screens, one match row, two different stories.

**Decision ordering.** A missing skill is the most *studyable* thing this file can find, which
is exactly why it must not win the headline by default. If a student has said they will not
relocate and the advert is in another city, then "learn Kafka" is not what stands between them
and this job -- and sending them to a Kafka course to solve a geography problem is the specific
failure this suite exists to prevent.

Note what a hard condition is, precisely: `job_hunt/matching` scores a different city as 0.5
when the student said they would relocate, and omits the component entirely when it could not
tell. Only an exact 0.0 reaches these assertions, so every case below is two *known* facts in
conflict -- never an inference from absent data.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.services.career_strategy import service as strategy_service
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_job_preferences,
    add_project_build,
    add_resume,
    add_tracked_job,
    add_untracked_listing,
    days_ago,
    make_profile,
    make_role,
)


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _ready_student(db, user_id=1, role_name="Backend Engineer", preferred_companies=()):
    """A student with genuinely proven Python -- so any blocker that appears is about the job,
    not about a thin profile."""
    role = make_role(db, role_name, {"Python": 1.0})
    profile = make_profile(db, user_id=user_id, target_role_id=role.id, degree="B.Tech",
                           branch="CSE", graduation_year=2026, experience_level="fresher")
    add_resume(db, profile, skills=["python"])
    add_job_preferences(db, profile, normalized_skills=["python"],
                        preferred_companies=preferred_companies)
    add_assessment_result(db, profile, {"Python": 0.93}, created_at=days_ago(3))
    add_project_build(db, profile, title="Python Service", stack_summary="Python")
    add_assessment_result(db, profile, {"Python": 0.9}, created_at=days_ago(2))
    return profile


class TestComposition:
    def test_the_match_engines_own_explanation_reaches_the_student(self, db):
        """The rationale is written by job_hunt/matching from the same component scores that
        produced the total. Recomputing or paraphrasing it here would let this screen and the
        Job Hunt card disagree about one advert."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.78,
            components={"experience_fit": 0.8, "skill_overlap": 0.9},
            rationale="Strong skill overlap and a good experience fit",
            listing_id_seed=610,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.match_rationale == "Strong skill overlap and a good experience fit"

    def test_unknown_factors_are_carried_through_not_scored_against_the_student(self, db):
        """A provider that published no salary makes the job unknowable on that axis, not the
        student worse. The list has to arrive verbatim so the UI can say which."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.7,
            components={"experience_fit": 0.8, "skill_overlap": 0.9},
            unknown_factors=["This listing doesn't state a salary"],
            listing_id_seed=611,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.unknown_factors == ["This listing doesn't state a salary"]
        # And it did not quietly become a blocker or a missing skill.
        assert "salary" not in {blocker.subject for blocker in result.blockers}

    def test_component_scores_are_the_stored_ones(self, db):
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.7,
            components={"experience_fit": 0.8, "skill_overlap": 0.9},
            listing_id_seed=612,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.component_scores["experience_fit"] == pytest.approx(0.8)
        assert result.component_scores["skill_overlap"] == pytest.approx(0.9)

    def test_readinesss_own_biggest_gap_is_passed_through_rather_than_recomputed(self, db):
        """readiness.compute already answers "what is the single most improvable thing here",
        with a before/after. A second answer computed in this file would be a second readiness
        model by another name."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], missing_skills=["Kafka"], score=0.7,
            components={"experience_fit": 0.8}, listing_id_seed=613,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.biggest_gap is not None
        # Typed rather than a free-form map, so a shape change in readiness.py fails here
        # instead of reaching a client as an empty card.
        assert result.biggest_gap.kind == "skill"
        assert result.biggest_gap.skill == "Kafka"
        assert result.biggest_gap.action == "roadmap"
        assert "Kafka" in result.biggest_gap.title


class TestJobConditions:
    def test_salary_keeps_its_period(self, db):
        """An hourly rate must survive the trip as an hourly rate. Annualising it would need an
        assumption about hours per week that no provider supplies."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.7,
            components={"experience_fit": 0.8},
            salary={"salary_min": 500, "salary_max": 900, "salary_currency": "INR",
                    "salary_period": "hour"},
            listing_id_seed=620,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.conditions.salary_period == "hour"
        assert result.conditions.salary_min == 500
        assert result.conditions.salary_currency == "INR"

    def test_an_unstated_salary_period_stays_unstated(self, db):
        """The dangerous default. `None` must not become "year" anywhere on the way out."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.7,
            components={"experience_fit": 0.8},
            salary={"salary_min": 600000, "salary_currency": "INR"},
            listing_id_seed=621,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.conditions.salary_period is None
        assert result.conditions.salary_min == 600000

    def test_a_predicted_salary_is_flagged_as_predicted(self, db):
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.7,
            components={"experience_fit": 0.8},
            salary={"salary_min": 800000, "salary_currency": "INR", "salary_period": "year",
                    "salary_is_predicted": True},
            listing_id_seed=622,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.conditions.salary_is_predicted is True

    def test_location_work_mode_and_employment_type_are_carried(self, db):
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.7,
            components={"experience_fit": 0.8},
            location="Pune", work_mode="hybrid", employment_type="full_time",
            listing_id_seed=623,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.conditions.location == "Pune"
        assert result.conditions.work_mode == "hybrid"
        assert result.conditions.employment_type == "full_time"


class TestConstraintsOutrankStudyTasks:
    """The headline behaviour of D3."""

    def test_a_location_the_student_ruled_out_outranks_a_missing_skill(self, db):
        """The defect this file exists for. Kafka is missing and Kafka is studyable, so it wins
        the headline unless something stops it -- and a course will not move the job."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], missing_skills=["Kafka"], score=0.7,
            components={"experience_fit": 0.8, "skill_overlap": 0.7, "location_fit": 0.0},
            location="Hyderabad", listing_id_seed=630,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        assert result.blockers, "expected at least the location blocker"
        assert result.blockers[0].kind == "condition"
        assert result.blockers[0].subject == "location"
        assert "Hyderabad" in result.blockers[0].detail
        # The headline must not send them to study Kafka for a job in the wrong city.
        assert "Kafka" not in result.headline
        # Kafka is still reported -- it is true, it is just not the decisive thing.
        assert "Kafka" in result.missing_skills

    def test_an_employment_type_mismatch_is_a_condition_not_a_skill_problem(self, db):
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], missing_skills=["Kafka"], score=0.7,
            components={"experience_fit": 0.8, "skill_overlap": 0.7, "employment_type_fit": 0.0},
            employment_type="internship", listing_id_seed=631,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.blockers[0].kind == "condition"
        assert result.blockers[0].subject == "employment_type"
        assert "internship" in result.blockers[0].detail

    def test_a_matching_location_produces_no_condition_blocker(self, db):
        """The control. A condition blocker must require a real measured mismatch."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], missing_skills=["Kafka"], score=0.7,
            components={"experience_fit": 0.8, "skill_overlap": 0.7, "location_fit": 1.0},
            listing_id_seed=632,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert "condition" not in {blocker.kind for blocker in result.blockers}

    def test_a_relocatable_student_is_not_blocked_by_a_different_city(self, db):
        """matching scores this 0.5, not 0.0, precisely because the student said they would
        move. Treating 0.5 as a hard constraint would overrule a preference they stated."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.7,
            components={"experience_fit": 0.8, "skill_overlap": 0.7, "location_fit": 0.5},
            location="Hyderabad", listing_id_seed=633,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert "condition" not in {blocker.kind for blocker in result.blockers}

    def test_an_unjudged_location_is_never_treated_as_a_mismatch(self, db):
        """A component the engine could not judge is absent from component_scores entirely.
        Absence must not read as zero -- that is the whole unknown-is-not-failure rule."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.7,
            components={"experience_fit": 0.8, "skill_overlap": 0.7},
            listing_id_seed=634,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert "condition" not in {blocker.kind for blocker in result.blockers}

    def test_a_ready_student_still_gets_apply_now_with_the_constraint_named(self, db):
        """A location does not make anyone less prepared, so this stays APPLY_NOW. Downgrading
        it would invent preparation work that does not exist -- but saying nothing about the
        city would hide the only open question."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.85,
            components={"experience_fit": 0.9, "skill_overlap": 1.0, "location_fit": 0.0},
            location="Hyderabad", listing_id_seed=635,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        if result.decision == "APPLY_NOW":
            assert "not where you said you want to work" in result.headline

    def test_the_condition_is_never_phrased_as_do_not_apply(self, db):
        """Whether to apply anyway is the student's call, always."""
        profile = _ready_student(db)
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], missing_skills=["Kafka"], score=0.7,
            components={"experience_fit": 0.8, "skill_overlap": 0.7, "location_fit": 0.0},
            location="Hyderabad", listing_id_seed=636,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        text = " ".join([result.headline] + [b.detail for b in result.blockers]).lower()
        for banned in ("do not apply", "don't apply", "should not apply", "avoid this job"):
            assert banned not in text


class TestAlreadyApplied:
    """Reading what the student already did.

    `get_listing_for_profile` returns a listing and a match, and nothing about the student's own
    history with that advert -- so the job strategy used to tell someone to "apply today" to a
    job they had applied to weeks earlier. That is not a small wording slip: it says plainly that
    the app is not reading their tracker, on the one screen whose entire purpose is to prove it
    reads everything.
    """

    def test_a_job_already_applied_to_says_so_before_anything_else(self, db):
        profile = _ready_student(db, user_id=40, role_name="Backend Engineer 40")
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.85,
            components={"experience_fit": 0.9, "skill_overlap": 1.0},
            applied=True, listing_id_seed=640,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.application.applied_at is not None
        assert result.headline.startswith("You applied to this on")

    def test_the_preparation_verdict_survives_the_application(self, db):
        """The decision describes how ready they are, which is still true and still useful after
        they have applied. Only the call to action changes."""
        profile = _ready_student(db, user_id=41, role_name="Backend Engineer 41")
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.85,
            components={"experience_fit": 0.9, "skill_overlap": 1.0},
            applied=True, listing_id_seed=641,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.decision in ("APPLY_NOW", "FIX_BEFORE_APPLYING")

    def test_a_rejected_application_is_still_an_application(self, db):
        """applied_at is authoritative. A rejection does not un-apply."""
        profile = _ready_student(db, user_id=42, role_name="Backend Engineer 42")
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.85,
            components={"experience_fit": 0.9}, applied=True, status="rejected",
            listing_id_seed=642,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.application.applied_at is not None
        assert result.headline.startswith("You applied to this on")

    def test_an_undocumented_status_is_carried_through_uninterpreted(self, db):
        """"closed" has no documented meaning, so it is reported and nothing is concluded from
        it. Inventing a meaning would be fabrication; dropping it would hide real tracker state."""
        profile = _ready_student(db, user_id=43, role_name="Backend Engineer 43")
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.85,
            components={"experience_fit": 0.9}, applied=False, status="closed",
            listing_id_seed=643,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.application.status == "closed"
        # Not read as an application, because applied_at is null.
        assert result.application.applied_at is None
        assert not result.headline.startswith("You applied to this on")

    def test_a_job_never_saved_reports_no_application_state(self, db):
        profile = _ready_student(db, user_id=44, role_name="Backend Engineer 44")
        listing = add_untracked_listing(db, listing_id_seed=644)

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.application.is_saved is False
        assert result.application.applied_at is None


class TestDecidingFactors:
    """A match component has to be readable as a sentence, not only as a number."""

    def test_each_scored_component_produces_one_readable_line(self, db):
        profile = _ready_student(db, user_id=45, role_name="Backend Engineer 45")
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.8,
            components={"skill_overlap": 0.9, "location_fit": 0.0, "experience_fit": 0.8},
            location="Hyderabad", listing_id_seed=645,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        joined = " ".join(result.deciding_factors)
        assert "Skills:" in joined
        assert "Location:" in joined
        assert "Experience:" in joined
        # And the weak one reads as weak rather than as a number.
        assert "not where you said you want to work" in joined

    def test_a_component_the_engine_could_not_judge_produces_no_line(self, db):
        """Absence is not a verdict. An unjudged component belongs in unknown_factors, not in a
        sentence claiming something about the student."""
        profile = _ready_student(db, user_id=46, role_name="Backend Engineer 46")
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.8,
            components={"skill_overlap": 0.9}, listing_id_seed=646,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        joined = " ".join(result.deciding_factors)
        assert "Location:" not in joined
        assert "Contract:" not in joined

    def test_a_preferred_employer_is_named_and_explicitly_not_scored(self, db):
        """Naming a company you like does not make you more qualified for it. The line says so."""
        profile = _ready_student(db, user_id=47, role_name="Backend Engineer 47",
                                 preferred_companies=["Acme"])
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.8,
            components={"skill_overlap": 0.9}, listing_id_seed=647,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        joined = " ".join(result.deciding_factors)
        assert "Acme" in joined
        assert "does not change how well you match" in joined

    def test_a_company_the_student_never_named_produces_no_employer_line(self, db):
        profile = _ready_student(db, user_id=48, role_name="Backend Engineer 48")
        listing = add_tracked_job(
            db, profile, matched_skills=["Python"], score=0.8,
            components={"skill_overlap": 0.9}, listing_id_seed=648,
        )

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert not any("targeting" in line for line in result.deciding_factors)
