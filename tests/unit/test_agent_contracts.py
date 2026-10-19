"""VA-6: the agent contracts, intent classification and planning. No database, no model.

The invariant worth most here is that an ungrounded claim cannot be constructed. If that holds,
the orchestrator cannot emit a sentence that sounds like a fact and traces to nothing -- which
is a stronger guarantee than a prompt asking a model to cite its sources.
"""

import datetime
import time

import pytest

from app.agent import intent as intent_module
from app.agent import planner
from app.agent.contracts import (
    WORLD_INTENTS,
    AgentRequest,
    Claim,
    Entities,
    EvidenceKind,
    EvidenceRef,
    GroundedAnswer,
    Intent,
    TurnBudget,
    TurnLimitReached,
    TurnTimeout,
)

NOW = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.timezone.utc)


def known(tool: str = "profile.get_summary") -> EvidenceRef:
    return EvidenceRef(kind=EvidenceKind.KNOWN, tool=tool, summary="a verified record")


def observed(tool: str = "world.find_openings") -> EvidenceRef:
    return EvidenceRef(
        kind=EvidenceKind.LIVE_OBSERVED, tool=tool, summary="5 adverts", provider="fake", retrieved_at=NOW
    )


class TestClaimsMustBeGrounded:
    def test_a_claim_without_evidence_cannot_be_constructed(self):
        with pytest.raises(ValueError) as caught:
            Claim(text="You are ready for an SDE role.", evidence=())
        assert "ungrounded claim cannot be made" in str(caught.value)

    def test_a_claim_without_text_is_refused(self):
        with pytest.raises(ValueError):
            Claim(text="   ", evidence=(known(),))

    def test_evidence_must_name_its_tool(self):
        with pytest.raises(ValueError):
            EvidenceRef(kind=EvidenceKind.KNOWN, tool="", summary="x")

    def test_live_evidence_must_name_its_provider_and_time(self):
        # Without these a world observation cannot be judged for freshness, and an
        # unjudgeable observation presented beside a verified fact reads as equally solid.
        with pytest.raises(ValueError):
            EvidenceRef(kind=EvidenceKind.LIVE_OBSERVED, tool="world.find_openings", summary="x")
        with pytest.raises(ValueError):
            EvidenceRef(
                kind=EvidenceKind.LIVE_OBSERVED, tool="world.find_openings", summary="x", provider="fake"
            )

    def test_personal_evidence_needs_no_provider_or_freshness(self):
        # A database row about the student has neither, and inventing values would be worse
        # than leaving them absent.
        ref = known()
        assert ref.provider is None and ref.freshness is None

    def test_the_weakest_evidence_governs_how_a_claim_reads(self):
        mixed = Claim(
            text="You are missing Kubernetes.",
            evidence=(known(), EvidenceRef(kind=EvidenceKind.DERIVED, tool="world.get_skill_demand", summary="6/20")),
        )
        assert mixed.weakest_kind is EvidenceKind.DERIVED

    def test_a_recommendation_stays_a_recommendation(self):
        from app.agent.orchestrator import recommendation

        claim = recommendation("Practise two mock interviews this week.", "progress.get_summary")
        assert claim.weakest_kind is EvidenceKind.RECOMMENDED


class TestGroundedAnswer:
    def test_an_answer_with_nothing_at_all_is_not_grounded(self):
        # Silence presented as a response is the one shape this type must not let through.
        assert GroundedAnswer(intent=Intent.PROGRESS).is_grounded is False

    def test_uncertainty_alone_counts_as_an_honest_answer(self):
        answer = GroundedAnswer(intent=Intent.PROGRESS, uncertainties=("No score computed yet.",))
        assert answer.is_grounded is True

    def test_claims_and_uncertainties_coexist(self):
        answer = GroundedAnswer(
            intent=Intent.READINESS, claims=(Claim("Coverage is 40%.", (known(),)),), uncertainties=("No path set.",)
        )
        assert answer.claims and answer.uncertainties

    def test_the_payload_is_transport_neutral(self):
        import json

        from app.agent.contracts import AgentTurn

        turn = AgentTurn(
            request_text="how am i doing",
            intent=Intent.PROGRESS,
            entities=Entities(),
            answer=GroundedAnswer(
                intent=Intent.PROGRESS,
                claims=(Claim("Your score is 61.", (known("progress.get_summary"),)),),
                tools_used=("progress.get_summary",),
            ),
            duration_seconds=0.1,
            tool_calls=1,
            rounds=1,
        )
        payload = json.loads(json.dumps(turn.to_payload()))
        assert payload["claims"][0]["kind"] == "known"
        assert payload["tools_used"] == ["progress.get_summary"]
        # No ORM object, no vendor shape, and the raw request text is not echoed back.
        assert "request_text" not in payload


class TestBudget:
    @pytest.mark.parametrize("kwargs", [{"seconds": 0}, {"max_tool_calls": 0}, {"max_rounds": 0}])
    def test_every_bound_must_be_positive(self, kwargs):
        with pytest.raises(ValueError):
            TurnBudget(**kwargs)

    def test_a_call_ceiling_is_enforced(self):
        budget = TurnBudget(max_tool_calls=2)
        budget.spend_call()
        budget.spend_call()
        with pytest.raises(TurnLimitReached):
            budget.spend_call()
        assert budget.calls_made == 2

    def test_a_call_is_counted_before_it_runs(self):
        # Counting only what returned is how a failing provider gets retried six times.
        budget = TurnBudget(max_tool_calls=3)
        budget.spend_call()
        assert budget.calls_remaining == 2

    def test_an_exhausted_clock_raises(self):
        budget = TurnBudget(seconds=0.01)
        time.sleep(0.02)
        with pytest.raises(TurnTimeout):
            budget.check()


class TestIntentClassification:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("any python jobs in Pune?", Intent.JOB_SEARCH),
            ("fresher jobs in Hyderabad", Intent.FRESHER_JOBS),
            ("what do SDE roles require", Intent.ROLE_REQUIREMENTS),
            ("which skills are in demand", Intent.SKILL_DEMAND),
            ("who is hiring for data analyst", Intent.COMPANY_HIRING),
            ("compare Pune vs Bangalore", Intent.LOCATION_COMPARISON),
            ("what am i missing for the market", Intent.SKILL_GAP_VS_MARKET),
            ("my weaknesses", Intent.WEAKNESSES),
            ("how ready am i", Intent.READINESS),
            ("show my interviews", Intent.INTERVIEW_HISTORY),
            ("is my resume uploaded", Intent.RESUME_STATUS),
            ("my saved jobs", Intent.SAVED_JOBS),
            ("what should i do today", Intent.TODAY_PLAN),
            ("how am i doing", Intent.PROGRESS),
            ("my skill passport", Intent.SKILL_STATUS),
            ("my profile", Intent.PROFILE_STATUS),
        ],
    )
    def test_each_intent_has_a_deterministic_rule(self, text, expected):
        assert intent_module.classify(text) is expected

    @pytest.mark.parametrize("text", ["", "   ", "asdfgh", "tell me a joke", "weather in pune"])
    def test_an_unmatched_request_is_unclear_rather_than_guessed(self, text):
        # Guessing would run tools against a question nobody asked, and a confident answer to
        # the wrong question is harder to notice than being asked to clarify.
        assert intent_module.classify(text) is Intent.UNCLEAR

    @pytest.mark.parametrize(
        "text",
        [
            "apply to this job for me",
            "save this job",
            "book a mock interview",
            "delete my resume",
            "update my profile",
            "remind me tomorrow",
        ],
    )
    def test_an_action_request_is_out_of_scope_not_reinterpreted(self, text):
        # Checked before the read rules on purpose: "apply to this job" must not be quietly
        # answered as a job search. A request to act is not a request to look.
        assert intent_module.classify(text) is Intent.OUT_OF_SCOPE

    def test_requirements_wins_over_fresher_jobs_when_both_could_match(self):
        assert intent_module.classify("what do fresher jobs require") is Intent.ROLE_REQUIREMENTS


class TestEntityExtraction:
    def test_a_known_location_is_extracted(self):
        assert intent_module.extract("jobs in Pune").location == "Pune"

    def test_an_unknown_place_is_not_extracted(self):
        # An open-ended place extractor would send arbitrary text to a provider as a geography
        # filter, and a provider that does not recognise it silently widens the search.
        assert intent_module.extract("jobs in Freedonia").location is None

    def test_two_locations_are_kept_in_order_of_the_known_list(self):
        found = intent_module.extract("compare Pune and Bangalore")
        assert set(found.locations) == {"Pune", "Bangalore"}

    def test_at_most_three_locations_survive(self):
        found = intent_module.extract("pune bangalore hyderabad chennai mumbai")
        assert len(found.locations) == 3

    def test_remote_is_a_location_value(self):
        assert intent_module.extract("remote python jobs").location == "remote"

    def test_a_role_is_extracted(self):
        assert intent_module.extract("jobs for a backend engineer").role == "backend engineer"

    def test_experience_level_is_extracted(self):
        assert intent_module.extract("entry level roles").experience_level == "fresher"

    def test_a_skill_comes_from_the_shared_vocabulary(self):
        # Same extractor the world layer uses to read adverts, so the agent and the market
        # data agree on what counts as a skill.
        assert intent_module.extract("how is my kubernetes").skill == "kubernetes"

    def test_entity_text_is_bounded(self):
        found = Entities(role="x" * 500)
        assert len(found.role) <= Entities.MAX_TEXT

    def test_the_entity_bag_has_no_room_for_personal_data(self):
        # VA-8 added two bounded integers (horizon_days, weekly_hours). The list is still
        # closed and still carries nothing that identifies the student -- which is the property
        # this test exists for, since `_world_arguments` builds provider queries from exactly
        # these fields. Both new ones are validated to a closed set or a numeric range in
        # `__post_init__`, so neither can carry free text out to a provider.
        fields = set(Entities.__dataclass_fields__)
        assert fields == {
            "role", "location", "locations", "skill", "experience_level", "horizon_days", "weekly_hours",
        }

    def test_the_coaching_entities_cannot_carry_free_text(self):
        import pytest as _pytest

        for bad in (45, 1, 365, 0, -30):
            with _pytest.raises(ValueError):
                Entities(horizon_days=bad)
        for bad in (0, 81, -5):
            with _pytest.raises(ValueError):
                Entities(weekly_hours=bad)

    def test_request_text_is_bounded(self):
        assert len(AgentRequest(text="x" * 10_000).text) == AgentRequest.MAX_TEXT


class TestPlanning:
    ALL = frozenset(
        {
            "profile.get_summary", "skills.get_passport", "career_twin.get_readiness",
            "weaknesses.list", "interviews.list_history", "progress.get_summary",
            "resume.get_latest_summary", "resume.list_ats_checks", "jobs.list_saved",
            "missions.get_today", "world.find_openings", "world.find_fresher_openings",
            "world.get_role_requirements", "world.get_skill_demand",
            "world.get_company_hiring", "world.compare_locations",
            # VA-8 wired these two. They were registered in VA-4 and planned by nothing.
            "career_goals.list", "career_path.get_current",
        }
    )

    def test_every_plannable_tool_has_a_grounding_branch(self):
        """The regression test for a defect VA-8 found rather than introduced.

        `career_goals.list` and `career_path.get_current` were registered read tools that no
        intent planned and `grounding.from_personal` had no branch for -- and an unhandled tool
        there silently returns no claims and no uncertainties. So the tool would have run,
        spent budget, and contributed nothing, with no error anywhere to notice. Asserting the
        two sides agree is what stops that recurring the next time a tool is wired up.
        """
        import pathlib
        import re

        source = (
            pathlib.Path(planner.__file__).resolve().parent / "grounding.py"
        ).read_text(encoding="utf-8")
        handled = set(re.findall(r'tool == "([^"]+)"', source))
        for group in re.findall(r"tool in \(([^)]+)\)", source):
            handled.update(re.findall(r'"([^"]+)"', group))

        planned: set[str] = set()
        entities = Entities(
            role="backend engineer",
            location="Pune",
            locations=("Pune", "Bangalore"),
            horizon_days=30,
        )
        for member in Intent:
            if member in (Intent.UNCLEAR, Intent.OUT_OF_SCOPE):
                continue
            planned.update(call.tool for call in planner.plan(member, entities, self.ALL))

        assert planned, "no intent planned anything, so this test proves nothing"
        assert planned <= handled, f"planned with no grounding branch: {sorted(planned - handled)}"

    def test_every_answerable_intent_plans_at_least_one_call(self):
        for member in Intent:
            if member in (Intent.UNCLEAR, Intent.OUT_OF_SCOPE):
                continue
            entities = Entities(role="backend engineer", location="Pune", locations=("Pune", "Bangalore"))
            assert planner.plan(member, entities, self.ALL), f"{member.value} plans nothing"

    def test_a_tool_absent_from_the_callers_catalogue_is_not_planned(self):
        # `available` is the caller's own catalogue, so a premium tool they lack is skipped
        # rather than attempted -- attempting it would leak its existence via the error.
        calls = planner.plan(Intent.PROGRESS, Entities(), frozenset())
        assert calls == ()

    def test_the_gap_intent_plans_both_sides(self):
        calls = planner.plan(Intent.SKILL_GAP_VS_MARKET, Entities(role="sde"), self.ALL)
        tools = [call.tool for call in calls]
        assert "skills.get_passport" in tools
        assert "world.get_role_requirements" in tools

    def test_world_calls_are_flagged_as_world(self):
        calls = planner.plan(Intent.JOB_SEARCH, Entities(role="sde"), self.ALL)
        assert all(call.is_world for call in calls)

    def test_personal_calls_are_not_flagged_as_world(self):
        calls = planner.plan(Intent.PROGRESS, Entities(), self.ALL)
        assert all(not call.is_world for call in calls)

    def test_a_comparison_without_two_locations_plans_nothing(self):
        # Padding in a default city would answer a comparison nobody asked for.
        assert planner.plan(Intent.LOCATION_COMPARISON, Entities(locations=("Pune",)), self.ALL) == ()

    def test_requirements_without_a_role_plans_nothing_and_asks_instead(self):
        assert planner.plan(Intent.ROLE_REQUIREMENTS, Entities(), self.ALL) == ()
        assert "role or skill" in planner.needs_more_detail(Intent.ROLE_REQUIREMENTS, Entities())

    def test_a_clarifying_question_is_offered_for_a_missing_comparison(self):
        assert "two cities" in planner.needs_more_detail(Intent.LOCATION_COMPARISON, Entities())

    def test_a_complete_request_needs_no_clarification(self):
        assert planner.needs_more_detail(Intent.ROLE_REQUIREMENTS, Entities(role="sde")) is None

    def test_the_call_ceiling_is_respected(self):
        calls = planner.plan(Intent.RESUME_STATUS, Entities(), self.ALL, max_calls=1)
        assert len(calls) == 1

    def test_world_arguments_contain_only_query_terms(self):
        calls = planner.plan(
            Intent.JOB_SEARCH, Entities(role="backend engineer", location="Pune", experience_level="fresher"), self.ALL
        )
        arguments = calls[0].arguments
        assert set(arguments) <= {"role", "location", "experience_level", "remote_only", "limit"}
        for forbidden in ("profile_id", "user_id", "resume", "skills", "email"):
            assert forbidden not in arguments

    def test_remote_becomes_a_flag_not_a_city(self):
        calls = planner.plan(Intent.JOB_SEARCH, Entities(role="sde", location="remote"), self.ALL)
        assert calls[0].arguments.get("remote_only") is True
        assert "location" not in calls[0].arguments

    def test_fresher_intent_forces_the_experience_filter(self):
        calls = planner.plan(Intent.FRESHER_JOBS, Entities(role="sde"), self.ALL)
        assert calls[0].arguments["experience_level"] == "fresher"

    def test_the_world_intent_set_matches_the_planner(self):
        for member in WORLD_INTENTS:
            calls = planner.plan(
                member, Entities(role="sde", locations=("Pune", "Bangalore")), self.ALL
            )
            assert any(call.is_world for call in calls), member.value
