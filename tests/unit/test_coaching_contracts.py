"""VA-8: the coaching contracts, with no database, no provider and no credentials.

The authorization and retrieval boundaries are tested for real in
`tests/api/test_voice_career_coach.py`. This file covers what must hold before any call
happens: a milestone cannot exist without evidence, cannot promise an outcome, a plan cannot
exceed its horizon's budget silently, and the five evidence categories cannot be re-decided
into disagreement with the grounding layer.

These are the properties a prompt cannot guarantee, which is why they are a data structure.
"""

import datetime
import types

import pytest

from app.agent.coaching import (
    HORIZONS,
    CoachingPlan,
    CoachingReport,
    Milestone,
    MilestoneKind,
    build_plan,
    next_step,
    role_fit,
)
from app.agent.contracts import Claim, EvidenceKind, EvidenceRef, GroundedAnswer, Intent

NOW = datetime.datetime(2026, 10, 12, 9, 0, tzinfo=datetime.timezone.utc)


def ref(kind: EvidenceKind = EvidenceKind.KNOWN, tool: str = "weaknesses.list", **extra) -> EvidenceRef:
    return EvidenceRef(kind=kind, tool=tool, summary="a reason", **extra)


def milestone(**overrides) -> Milestone:
    defaults = dict(
        horizon_days=30,
        kind=MilestoneKind.PRACTICE,
        title="Drill SQL joins",
        why="Flagged three times in your own assessments.",
        evidence=(ref(),),
        effort_hours=4,
    )
    defaults.update(overrides)
    return Milestone(**defaults)


# --- synthetic tool outputs. Plain namespaces, so these tests exercise the coaching logic and
# --- not the read tools' serialisation, which has its own tests.
def readiness(**kw):
    base = dict(
        has_role=True,
        display_role_name="SDE",
        evidence_coverage_percent=42,
        is_estimate=False,
        biggest_gap_skill="System Design",
        next_action_hint="Complete the DSA assessment.",
        applications_total=3,
        applications_at_interview_stage=1,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def passport(*skills):
    return types.SimpleNamespace(
        skills=[types.SimpleNamespace(skill=s, confidence_percent=c) for s, c in skills]
    )


def weaknesses(*items):
    # `skill_key` is the field `read_tools.WeaknessItem` actually exposes. An earlier version
    # of this helper used `skill`, which made these tests pass while the production code read
    # nothing -- the exact shape of mistake a fake is supposed not to introduce.
    return types.SimpleNamespace(
        weaknesses=[types.SimpleNamespace(skill_key=s, occurrences=n) for s, n in items]
    )


def goals(*items):
    return types.SimpleNamespace(
        goals=[types.SimpleNamespace(title=t, company=None, is_analyzed=a) for t, a in items]
    )


def requirements(*rows):
    return types.SimpleNamespace(
        skills=[
            types.SimpleNamespace(skill=s, openings_mentioning=h, sample_size=n) for s, h, n in rows
        ]
    )


# --------------------------------------------------------------------------- milestones


class TestAMilestoneCannotBeUngrounded:
    def test_evidence_is_required(self):
        with pytest.raises(ValueError) as caught:
            milestone(evidence=())
        assert "no evidence" in str(caught.value)

    def test_a_title_and_a_reason_are_required(self):
        with pytest.raises(ValueError):
            milestone(title="   ")
        with pytest.raises(ValueError):
            milestone(why="")

    def test_the_weakest_evidence_governs_how_it_reads(self):
        built = milestone(evidence=(ref(EvidenceKind.KNOWN), ref(EvidenceKind.DERIVED)))
        assert built.weakest_kind is EvidenceKind.DERIVED

    @pytest.mark.parametrize(
        "text",
        [
            "This will get you a job at Google",
            "Guaranteed placement after this step",
            "This ensures you an interview",
            "You will be placed within 30 days",
            "100% success rate on this track",
            "An assured offer follows",
        ],
    )
    def test_a_promise_about_an_outcome_cannot_be_constructed(self, text):
        # Not a style rule. "You will get a job" is a prediction about a hiring decision made
        # by people this system has never seen, and the cost of being believed lands on a
        # student deciding how to spend their months.
        with pytest.raises(ValueError) as caught:
            milestone(why=text)
        assert "promises an outcome" in str(caught.value)
        with pytest.raises(ValueError):
            milestone(title=text)

    def test_a_legitimate_reason_that_merely_sounds_confident_is_allowed(self):
        # The ban must not swallow honest language, or it would push the writing towards vague.
        assert milestone(why="This is the largest recorded gap for your target role.") is not None

    @pytest.mark.parametrize("bad", [45, 1, 0, -30, 365])
    def test_an_unsupported_horizon_is_refused(self, bad):
        with pytest.raises(ValueError):
            milestone(horizon_days=bad)

    def test_a_non_positive_effort_is_refused(self):
        with pytest.raises(ValueError):
            milestone(effort_hours=0)


# --------------------------------------------------------------------------- plans


class TestPlanShape:
    OUTPUTS = {
        "career_twin.get_readiness": readiness(),
        "skills.get_passport": passport(("python", 80), ("sql", 40)),
        "weaknesses.list": weaknesses(("SQL joins", 3), ("Recursion", 2), ("Graphs", 2)),
        "career_goals.list": goals(("Backend Engineer", False)),
        "world.get_role_requirements": requirements(("docker", 7, 12), ("kubernetes", 5, 12), ("python", 11, 12)),
    }

    @pytest.mark.parametrize("horizon", HORIZONS)
    def test_every_horizon_is_bounded_and_ordered(self, horizon):
        plan = build_plan(horizon, self.OUTPUTS)
        assert plan.horizon_days == horizon
        assert all(m.horizon_days == horizon for m in plan.milestones)
        # Evidence work first, process work last: a plan that opened with "analyse your goals"
        # would put the unblocking before the thing it unblocks.
        order = [m.kind for m in plan.milestones]
        assert order == sorted(order, key=lambda k: list(MilestoneKind).index(k))

    def test_a_short_horizon_holds_less_than_a_long_one(self):
        assert len(build_plan(7, self.OUTPUTS).milestones) < len(build_plan(90, self.OUTPUTS).milestones)
        assert len(build_plan(7, self.OUTPUTS).milestones) <= 2

    def test_what_did_not_fit_is_said_rather_than_dropped_silently(self):
        plan = build_plan(7, self.OUTPUTS)
        assert any("did not fit" in note for note in plan.uncertainties)

    def test_a_skill_the_student_already_has_is_not_a_learning_milestone(self):
        plan = build_plan(90, self.OUTPUTS)
        titles = " ".join(m.title.lower() for m in plan.milestones)
        # python is in the adverts and in the passport at 80%.
        assert "learn python" not in titles
        assert "kubernetes" in titles or "docker" in titles

    def test_a_market_milestone_names_the_sample_it_came_from(self):
        plan = build_plan(90, self.OUTPUTS)
        learned = [m for m in plan.milestones if m.kind is MilestoneKind.LEARN]
        assert learned, "no market-derived milestone was built"
        for m in learned:
            assert "adverts" in m.why
            # DERIVED, never KNOWN: it is a comparison this code made over a handful of adverts.
            assert m.weakest_kind is EvidenceKind.DERIVED

    def test_an_estimated_coverage_number_is_labelled_as_one(self):
        plan = build_plan(30, {**self.OUTPUTS, "career_twin.get_readiness": readiness(is_estimate=True)})
        evidence_steps = [m for m in plan.milestones if m.kind is MilestoneKind.EVIDENCE]
        assert evidence_steps and "(an estimate)" in evidence_steps[0].why

    def test_an_empty_account_gets_no_plan_and_an_explicit_reason(self):
        plan = build_plan(30, {})
        assert plan.milestones == ()
        assert any("not have enough recorded evidence" in note for note in plan.uncertainties)

    def test_a_plan_without_market_data_says_so(self):
        plan = build_plan(30, {k: v for k, v in self.OUTPUTS.items() if not k.startswith("world.")})
        assert any("only your own records" in note for note in plan.uncertainties)

    def test_a_plan_with_market_data_does_not_claim_it_lacks_it(self):
        plan = build_plan(30, self.OUTPUTS)
        assert not any("only your own records" in note for note in plan.uncertainties)

    def test_an_unsupported_horizon_is_refused(self):
        with pytest.raises(ValueError):
            build_plan(45, self.OUTPUTS)

    def test_the_same_evidence_always_gives_the_same_plan(self):
        first = build_plan(60, self.OUTPUTS)
        second = build_plan(60, self.OUTPUTS)
        assert [m.title for m in first.milestones] == [m.title for m in second.milestones]


class TestTimeBudget:
    OUTPUTS = TestPlanShape.OUTPUTS

    def test_no_stated_budget_means_no_claim_about_fit(self):
        plan = build_plan(30, self.OUTPUTS)
        assert plan.weekly_hours is None
        assert plan.capacity_hours is None
        assert plan.fits_time_budget is None

    def test_a_plan_that_does_not_fit_says_so_and_keeps_its_milestones(self):
        plan = build_plan(30, self.OUTPUTS, weekly_hours=1)
        assert plan.fits_time_budget is False
        # Reported, not resolved by truncation: a plan quietly shortened to fit would read as
        # achievable when it is no longer the plan the evidence supports.
        assert plan.milestones
        assert any("will not all fit" in note for note in plan.uncertainties)

    def test_a_plan_that_fits_makes_no_complaint(self):
        plan = build_plan(90, self.OUTPUTS, weekly_hours=40)
        assert plan.fits_time_budget is True
        assert not any("will not all fit" in note for note in plan.uncertainties)

    def test_capacity_scales_with_the_horizon(self):
        assert build_plan(7, self.OUTPUTS, weekly_hours=10).capacity_hours == 10
        assert build_plan(90, self.OUTPUTS, weekly_hours=10).capacity_hours == 128

    def test_a_milestone_cannot_belong_to_another_horizon(self):
        with pytest.raises(ValueError):
            CoachingPlan(horizon_days=30, milestones=(milestone(horizon_days=90),))


# --------------------------------------------------------------------------- next step / role fit


class TestNextStep:
    def test_an_open_mission_comes_first(self):
        claims, unknown = next_step(
            {
                "missions.get_today": types.SimpleNamespace(has_mission=True, title="Two DSA problems", status="pending"),
                "career_twin.get_readiness": readiness(),
            }
        )
        assert unknown == ()
        assert "Two DSA problems" in claims[0].text
        # A suggestion, typed as one, so it can never be read back as a fact about the student.
        assert claims[0].weakest_kind is EvidenceKind.RECOMMENDED

    def test_a_completed_mission_falls_through_to_the_next_best_thing(self):
        claims, _ = next_step(
            {
                "missions.get_today": types.SimpleNamespace(has_mission=True, title="Done thing", status="completed"),
                "career_twin.get_readiness": readiness(),
            }
        )
        assert "Complete the DSA assessment." in claims[0].text

    def test_the_top_weakness_is_the_last_resort(self):
        claims, _ = next_step(
            {
                "career_twin.get_readiness": readiness(next_action_hint=None),
                "weaknesses.list": weaknesses(("Recursion", 4)),
            }
        )
        assert "Recursion" in claims[0].text
        assert claims[0].weakest_kind is EvidenceKind.RECOMMENDED

    def test_an_empty_account_gets_an_unknown_not_a_guess(self):
        claims, unknown = next_step({})
        assert claims == ()
        assert "not have enough recorded progress" in unknown[0]


class TestRoleFit:
    def test_no_recorded_goals_means_nothing_to_compare(self):
        claims, unknown = role_fit({"career_twin.get_readiness": readiness()})
        assert claims == ()
        # Suggesting a role the student never mentioned would be the system choosing their
        # career for them on no evidence.
        assert "not recorded any career goals" in unknown[0]

    def test_an_aligned_goal_is_a_derived_comparison_not_a_fact(self):
        claims, _ = role_fit(
            {
                "career_goals.list": goals(("SDE at Acme", True)),
                "career_twin.get_readiness": readiness(),
            }
        )
        assert claims and claims[0].weakest_kind is EvidenceKind.DERIVED

    def test_a_mismatch_between_goals_and_target_role_is_named_not_resolved(self):
        claims, unknown = role_fit(
            {
                "career_goals.list": goals(("Data Analyst at Globex", True)),
                "career_twin.get_readiness": readiness(display_role_name="SDE"),
            }
        )
        assert claims == ()
        assert any("out of date" in note for note in unknown)

    def test_unanalysed_goals_limit_the_comparison_explicitly(self):
        _claims, unknown = role_fit(
            {
                "career_goals.list": goals(("SDE at Acme", False)),
                "career_twin.get_readiness": readiness(),
            }
        )
        assert any("has been analysed" in note or "cannot compare" in note for note in unknown)

    def test_no_readiness_means_no_baseline_and_it_is_stated(self):
        _claims, unknown = role_fit({"career_goals.list": goals(("SDE at Acme", True))})
        assert any("no baseline" in note for note in unknown)


# --------------------------------------------------------------------------- the report


class TestTheReportPartitionsRatherThanReDecides:
    ANSWER = GroundedAnswer(
        intent=Intent.READINESS,
        claims=(
            Claim("A stored fact.", (ref(EvidenceKind.KNOWN, "profile.get_summary"),)),
            Claim(
                "A retrieved observation.",
                (
                    EvidenceRef(
                        kind=EvidenceKind.LIVE_OBSERVED,
                        tool="world.find_openings",
                        summary="3 adverts",
                        provider="fake",
                        retrieved_at=NOW,
                        freshness="fresh",
                    ),
                ),
            ),
            Claim("A derived comparison.", (ref(EvidenceKind.DERIVED, "world.get_role_requirements"),)),
            Claim("A suggestion.", (ref(EvidenceKind.RECOMMENDED, "weaknesses.list"),)),
        ),
        uncertainties=("Something could not be established.",),
        tools_used=("profile.get_summary",),
        failures=("world.get_skill_demand is not available to this account.",),
    )

    def test_each_category_holds_exactly_its_own_claims(self):
        report = CoachingReport.from_answer(self.ANSWER)
        assert [c.text for c in report.stored_facts] == ["A stored fact."]
        assert [c.text for c in report.retrieved_observations] == ["A retrieved observation."]
        assert [c.text for c in report.derived_analysis] == ["A derived comparison."]
        assert [c.text for c in report.recommendations] == ["A suggestion."]

    def test_a_recommendation_never_appears_among_the_facts(self):
        report = CoachingReport.from_answer(self.ANSWER)
        facts = " ".join(c.text for c in report.stored_facts + report.retrieved_observations)
        assert "A suggestion." not in facts

    def test_the_partition_follows_the_weakest_evidence(self):
        # A claim built from a personal record *and* a market sample is derived, not known --
        # so a joined conclusion cannot be promoted into a fact about the student by sitting
        # next to one.
        joined = Claim(
            "A joined conclusion.",
            (ref(EvidenceKind.KNOWN, "skills.get_passport"), ref(EvidenceKind.DERIVED, "world.get_role_requirements")),
        )
        report = CoachingReport.from_answer(GroundedAnswer(intent=Intent.SKILL_GAP_VS_MARKET, claims=(joined,)))
        assert report.stored_facts == ()
        assert [c.text for c in report.derived_analysis] == ["A joined conclusion."]

    def test_an_unknown_kind_claim_is_reported_as_an_unknown_not_a_fact(self):
        report = CoachingReport.from_answer(
            GroundedAnswer(
                intent=Intent.READINESS,
                claims=(Claim("We looked and cannot say.", (ref(EvidenceKind.UNKNOWN, "none"),)),),
            )
        )
        assert report.stored_facts == ()
        assert "We looked and cannot say." in report.unknowns

    def test_uncertainties_and_failures_are_carried_through_unchanged(self):
        report = CoachingReport.from_answer(self.ANSWER)
        assert "Something could not be established." in report.unknowns
        assert report.failures == self.ANSWER.failures

    def test_freshness_is_reported_only_for_evidence_that_has_a_provider(self):
        report = CoachingReport.from_answer(self.ANSWER)
        assert len(report.freshness_notes) == 1
        note = report.freshness_notes[0]
        assert "world.find_openings" in note and "fake" in note and "fresh" in note
        # A personal database row has no freshness policy, and attaching one would imply a
        # retrieval that never happened.
        assert "profile.get_summary" not in note

    def test_the_payload_is_transport_neutral_and_carries_the_basis_of_every_claim(self):
        payload = CoachingReport.from_answer(self.ANSWER, plan=build_plan(30, TestPlanShape.OUTPUTS)).to_payload()
        assert set(payload) == {
            "intent", "stored_facts", "retrieved_observations", "derived_analysis", "recommendations",
            "unknowns", "failures", "tools_used", "clarifying_question", "freshness", "plan",
        }
        assert all("basis" in entry for entry in payload["stored_facts"] + payload["recommendations"])
        assert payload["plan"]["horizon_days"] == 30
        assert all("basis" in m for m in payload["plan"]["milestones"])

    def test_a_clarifying_question_alone_still_counts_as_content(self):
        report = CoachingReport.from_answer(
            GroundedAnswer(intent=Intent.CAREER_PLAN, clarifying_question="Over what period?")
        )
        assert report.has_content and report.plan is None
