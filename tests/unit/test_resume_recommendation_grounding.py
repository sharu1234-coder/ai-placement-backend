"""F-02: resume recommendations must be grounded in deterministic resume facts.

The Stage 5 audit found this section was the one part of the preparation plan whose claims
about the student's resume were protected only by prompt wording. Gap guidance and project
connections iterate a deterministic list and look the model's prose up by key, so an invented
entry is never looked up; this list is produced by the model itself and was copied through.

Every test here drives the real validator with structured output a hostile or hallucinating
model could genuinely return -- not with prompt text -- because the whole point of the fix is
that it holds regardless of what the model says.
"""

from app.ai.schemas.job_preparation_plan import GeneratedResumeRecommendation
from app.services import jd_grounding
from app.services.jd_grounding import GroundedGap, ResumeFacts, ResumeProject

_REQUIREMENTS = ["SQL", "Python", "Excel"]


def _facts() -> ResumeFacts:
    """A student who genuinely knows Python and built one named project, and nothing else."""
    return ResumeFacts(
        has_resume=True,
        skills=["Python", "Pandas"],
        projects=[
            ResumeProject(
                title="Bus Tracker",
                description="Live bus arrival board for the campus shuttle",
                tech_stack="Python, Flask",
                source="resume",
            )
        ],
        weak_sections=["summary"],
        experience_count=0,
        project_count=1,
        certification_count=0,
    )


def _gaps() -> list:
    """SQL is a deterministic gap -- the matcher established the student does not have it."""
    return [
        GroundedGap(
            skill="SQL",
            severity="high",
            is_explicit_requirement=True,
            requirement_importance="required",
            requirement_value="SQL",
        )
    ]


def _item(**overrides) -> GeneratedResumeRecommendation:
    base = dict(
        area="skills",
        requirement="Python",
        current_fact="Your resume lists Python and Pandas.",
        suggestion="Name the Python libraries next to the Bus Tracker project.",
        rationale="The job asks for Python specifically.",
    )
    base.update(overrides)
    return GeneratedResumeRecommendation(**base)


def _ground(items) -> list:
    return jd_grounding.ground_resume_recommendations(_facts(), _gaps(), _REQUIREMENTS, items)


class TestGroundedRecommendationsSurvive:
    def test_a_recommendation_restating_a_real_skill_is_kept(self):
        kept = _ground([_item()])
        assert len(kept) == 1
        assert kept[0].current_fact == "Your resume lists Python and Pandas."

    def test_a_recommendation_about_a_real_project_is_kept(self):
        kept = _ground([_item(current_fact="Your Bus Tracker project is described in one line.")])
        assert len(kept) == 1

    def test_a_recommendation_about_a_flagged_weak_section_is_kept(self):
        kept = _ground([_item(current_fact="Your summary section is thin.")])
        assert len(kept) == 1

    def test_the_absence_sentinel_is_kept_and_asserts_nothing(self):
        """The one claim that cannot overstate the student, so it bypasses the fact check."""
        kept = _ground([_item(current_fact="Not present in your resume")])
        assert len(kept) == 1

    def test_the_absence_sentinel_is_matched_regardless_of_case_or_final_stop(self):
        kept = _ground([_item(current_fact="not present in your resume.")])
        assert len(kept) == 1

    def test_a_small_ordinal_is_not_treated_as_an_invented_metric(self):
        """A count of bullet points is advice about the document, not a claim of achievement."""
        kept = _ground([_item(current_fact="Your Bus Tracker project has 2 bullet points.")])
        assert len(kept) == 1

    def test_a_null_requirement_is_allowed_for_general_presentation_advice(self):
        kept = _ground([_item(requirement=None, current_fact="Your summary section is thin.")])
        assert len(kept) == 1


class TestFabricatedRecommendationsAreDropped:
    def test_an_invented_skill_is_dropped(self):
        kept = _ground([_item(current_fact="Your resume shows strong Kubernetes administration.")])
        assert kept == []

    def test_a_claim_contradicting_a_deterministic_gap_is_dropped(self):
        """The most dangerous case: the matcher proved SQL is missing and the model says it is
        there. A student would repeat this to a recruiter."""
        kept = _ground([_item(current_fact="Your resume already demonstrates SQL on the Bus Tracker project.")])
        assert kept == []

    def test_invented_work_experience_is_dropped(self):
        """ResumeFacts deliberately carries no employer names, so an employer claim can never
        be corroborated -- which is exactly why it must not survive."""
        kept = _ground([_item(current_fact="Your internship at Infosys is listed under experience.")])
        assert kept == []

    def test_an_invented_metric_is_dropped(self):
        kept = _ground([_item(current_fact="Your Bus Tracker project reduced wait times by 40%.")])
        assert kept == []

    def test_an_invented_project_is_dropped(self):
        kept = _ground([_item(current_fact="Your Fraud Detection Pipeline project is listed first.")])
        assert kept == []

    def test_a_fact_citing_nothing_we_know_is_dropped(self):
        kept = _ground([_item(current_fact="Your resume is well structured overall.")])
        assert kept == []

    def test_a_requirement_the_jd_never_stated_is_dropped(self):
        """A claim about what the employer asked for is a fabrication of its own."""
        kept = _ground([_item(requirement="Kubernetes", current_fact="Your resume lists Python.")])
        assert kept == []

    def test_an_empty_current_fact_is_dropped(self):
        kept = _ground([_item(current_fact="   ")])
        assert kept == []

    def test_the_absence_sentinel_does_not_excuse_an_invented_requirement(self):
        kept = _ground([_item(requirement="Kubernetes", current_fact="Not present in your resume")])
        assert kept == []


class TestMixedOutput:
    def test_only_the_grounded_items_survive(self):
        items = [
            _item(current_fact="Your resume lists Python and Pandas."),
            _item(current_fact="Your resume already demonstrates SQL."),
            _item(current_fact="Your Bus Tracker project is described in one line."),
            _item(current_fact="Your internship at Infosys is listed under experience."),
            _item(current_fact="Not present in your resume"),
        ]
        kept = _ground(items)
        assert len(kept) == 3
        assert all("SQL" not in k.current_fact and "Infosys" not in k.current_fact for k in kept)

    def test_nothing_is_ever_repaired_or_substituted(self):
        """A dropped item must leave no trace -- no placeholder, no rewritten fact."""
        kept = _ground([_item(current_fact="Your resume shows strong Kubernetes administration.")])
        assert kept == []

    def test_an_empty_generation_grounds_to_nothing_without_error(self):
        assert _ground([]) == []


class TestNoResumeStillRefusesToInvent:
    def test_a_student_with_no_projects_or_skills_grounds_nothing(self):
        """Guards the boundary: with an empty fact vocabulary every positive claim is
        ungroundable, so the section must come back empty rather than pass prose through."""
        bare = ResumeFacts(has_resume=True, skills=[], projects=[], weak_sections=[])
        kept = jd_grounding.ground_resume_recommendations(
            bare, _gaps(), _REQUIREMENTS, [_item(current_fact="Your resume lists Python.")]
        )
        assert kept == []


class TestKnownFalseNegatives:
    """Cases the validator drops that a human would call honest.

    Recorded as tests rather than left as a surprise: each costs a usable recommendation and
    none can produce a false claim, which is the trade this module is built around. If a future
    change makes one of these survive, that is a deliberate decision and this test should be the
    thing that forces the conversation.
    """

    def test_an_honest_absence_phrased_freely_is_dropped_rather_than_interpreted(self):
        """"Your resume does not mention SQL" is true, and is still dropped.

        Telling it from "your resume demonstrates SQL" means reading the model's prose for
        intent, which is exactly the dependency this module removes. The sanctioned way to say
        something is missing is the sentinel.
        """
        kept = _ground([_item(current_fact="Your resume does not mention SQL anywhere.")])
        assert kept == []

    def test_a_real_fact_worded_without_any_known_term_is_dropped(self):
        """Grounding is by vocabulary, so a true statement that shares no token with the
        deterministic facts cannot be corroborated and does not survive."""
        kept = _ground([_item(current_fact="Your technical section is short.")])
        assert kept == []
