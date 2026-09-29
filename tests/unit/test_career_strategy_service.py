"""The unified service: composition, persistence, and the premium split.

The test that matters most in this file is `test_no_premium_content_is_present_in_a_free_
response`. The spec's rule is that premium data must not be sent to a free client and merely
hidden on Android, and the only way to hold that line over time is to take the premium
response, extract every string that only Premium should see, and prove none of them appear
anywhere in the free payload. A field-by-field assertion would pass happily on the day someone
adds a new premium field and forgets to gate it; this one fails.

The rest is about composition: that this layer sequences services rather than replacing them,
that a job decision does not silently write history, and that a student who cannot be judged is
told so rather than given a confident-looking number.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.placement_strategy_snapshot import PlacementStrategySnapshot
from app.schemas.career_strategy import LOCKABLE_SECTIONS
from app.services.career_strategy import job_strategy
from app.services.career_strategy import service as strategy_service
from app.services.job_hunt import service as job_hunt_service
from tests.fixtures.career_strategy_helpers import (
    NOW,
    add_assessment_result,
    add_interview_evaluation,
    add_job_preferences,
    add_project_build,
    add_resume,
    add_tracked_job,
    add_weakness_record,
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


def _rich_profile(db, user_id=1):
    """A student with enough recorded for the engine to reach a real decision: proven technical
    skills, a resume, tracked jobs, and a repeated interview weakness."""
    role = make_role(db, f"Data Engineer {user_id}", {"SQL": 1.0, "Python": 1.0, "Airflow": 0.8})
    profile = make_profile(db, user_id=user_id, target_role_id=role.id, degree="B.Tech",
                           branch="CSE", graduation_year=2026, experience_level="fresher")
    for skill in ("SQL", "Python"):
        add_assessment_result(db, profile, {skill: 0.9}, created_at=days_ago(4))
        add_project_build(db, profile, title=f"{skill} Pipeline", stack_summary=skill)
    add_resume(db, profile, skills=["sql", "python"])
    # Job Hunt has to know this student's skills, or provenance reports every unmatched advert
    # skill as UNCLEAR rather than MISSING -- correctly, since with no skill list there is no
    # way to tell a gap from an omission. See TestJobStrategy's unknown-skills test.
    add_job_preferences(db, profile, normalized_skills=["sql", "python"])
    add_interview_evaluation(db, profile, clarity=0.25, structure=0.3)
    add_weakness_record(db, profile, "interview:clarity", status="REPEATED", occurrences=3)
    add_tracked_job(db, profile, missing_skills=["Airflow"], matched_skills=["SQL"],
                    listing_id_seed=user_id * 10 + 1)
    return profile


class TestPipeline:
    def test_a_read_produces_a_decision_and_records_it(self, db):
        profile = _rich_profile(db)
        view = strategy_service.compute(db, profile, now=NOW)

        assert view.decision.primary is not None
        assert view.snapshot is not None
        assert view.snapshot_is_new is True
        assert view.snapshot.sequence == 1
        assert db.query(PlacementStrategySnapshot).count() == 1

    def test_opening_the_screen_repeatedly_writes_one_row(self, db):
        """The spec's rule stated as behaviour rather than as an implementation detail."""
        profile = _rich_profile(db)
        for _ in range(4):
            view = strategy_service.compute(db, profile, now=NOW)
        assert view.snapshot_is_new is False
        assert db.query(PlacementStrategySnapshot).count() == 1

    def test_the_pipeline_is_deterministic(self, db):
        profile = _rich_profile(db)
        first = strategy_service.compute(db, profile, now=NOW)
        second = strategy_service.compute(db, profile, now=NOW)
        assert first.decision.model_dump() == second.decision.model_dump()

    def test_readiness_comes_from_career_twin_not_from_a_second_calculation(self, db):
        """Composition, asserted rather than assumed: the number on this screen is the number
        Career Twin publishes. Two coverage figures disagreeing on two screens is precisely
        the failure the orchestration rule exists to prevent."""
        from app.services import career_twin_service

        profile = _rich_profile(db)
        view = strategy_service.compute(db, profile, now=NOW)
        assert (view.twin.evidence_coverage_percent
                == career_twin_service.get_current(db, profile).evidence_coverage_percent)

    def test_the_recommended_action_defers_to_next_best_action_when_they_agree(self, db):
        """Career Strategy does not run a second recommendation ladder. Where NBA already has an
        opinion about the same subject, its wording is what the student is shown."""
        from app.services import next_best_action_service

        profile = _rich_profile(db)
        view = strategy_service.compute(db, profile, now=NOW)
        nba = next_best_action_service.get_next_best_action(db, profile)
        if nba.has_recommendation and nba.skill == view.decision.primary.subject:
            assert view.decision.primary.recommended_action == nba.title

    def test_a_student_with_nothing_recorded_is_not_given_a_strategy(self, db):
        profile = make_profile(db)
        view = strategy_service.compute(db, profile, now=NOW)
        assert view.decision.state in ("FOCUS_ON_TARGET_ROLE", "INSUFFICIENT_INFORMATION")
        assert view.decision.primary is None
        # Still recorded: entering and leaving this state is itself a real transition.
        assert view.snapshot is not None

    def test_the_latest_change_survives_a_no_op_read(self, db):
        """After the strategy moves, opening the screen again must still show what changed --
        comparing the current snapshot against itself would tell a long-standing student they
        had just started."""
        profile = _rich_profile(db)
        strategy_service.compute(db, profile, now=NOW)
        add_assessment_result(db, profile, {"Airflow": 0.95}, created_at=days_ago(1))
        moved = strategy_service.compute(db, profile, now=NOW)
        assert moved.snapshot_is_new is True

        again = strategy_service.compute(db, profile, now=NOW)
        assert again.snapshot_is_new is False
        assert again.change.from_sequence == moved.change.from_sequence
        assert "first recorded strategy" not in " ".join(again.change.summary)


class TestPremiumProjection:
    def test_no_premium_content_is_present_in_a_free_response(self, db):
        """Take everything only Premium should see, and prove none of it reaches a free client.

        Deliberately written as a content scan rather than a field checklist: a checklist keeps
        passing on the day a new premium field is added and left ungated, which is the exact
        mistake this rule exists to catch.
        """
        profile = _rich_profile(db)
        view = strategy_service.compute(db, profile, now=NOW)

        premium = strategy_service.project(view, is_premium=True)
        free = strategy_service.project(view, is_premium=False)
        free_json = free.model_dump_json()

        # The bottleneck's own title and CTA are deliberately free (the spec's free preview is
        # "bottleneck name + next action + basic CTA"), and the top queue item is the bottleneck,
        # so those exact strings appearing in both responses is the design, not a leak. Excluded
        # by value rather than by field, so any *other* queue title still counts as a leak.
        deliberately_free = {
            free.state_reason,
            "" if free.bottleneck is None else free.bottleneck.title,
            "" if free.bottleneck is None else free.bottleneck.recommended_action,
        }

        premium_only = []
        if premium.bottleneck is not None:
            premium_only.extend(premium.bottleneck.evidence)
            premium_only.extend(premium.bottleneck.created_from_signals)
            premium_only.extend(premium.bottleneck.decision_signals)
            premium_only.append(premium.bottleneck.description)
            premium_only.append(premium.bottleneck.reason_code)
        premium_only.extend(item.title for item in premium.queue)
        premium_only.extend(item.reason for item in premium.queue)
        premium_only.extend(item.reason for item in premium.do_not_do_yet)
        premium_only.extend(item.explanation for item in premium.contradictions)
        premium_only.extend(item.reason for item in premium.diminishing_returns)
        if premium.latest_change is not None:
            premium_only.extend(premium.latest_change.summary)

        leaked = [
            value
            for value in premium_only
            if value and value not in deliberately_free and value in free_json
        ]
        assert leaked == [], f"premium content reached a free client: {leaked}"

    def test_a_free_student_still_gets_a_useful_screen(self, db):
        """The free tier is a real product, not a locked door. Role, evidence coverage, the name of
        bottleneck and one thing to do about it are all present."""
        profile = _rich_profile(db)
        free = strategy_service.project(strategy_service.compute(db, profile, now=NOW), is_premium=False)

        assert free.has_role is True
        assert free.display_role_name
        assert free.state
        assert free.bottleneck is not None
        assert free.bottleneck.title
        assert free.bottleneck.recommended_action
        assert free.bottleneck.destination

    def test_the_gated_sections_are_empty_and_named(self, db):
        profile = _rich_profile(db)
        free = strategy_service.project(strategy_service.compute(db, profile, now=NOW), is_premium=False)

        assert free.is_premium is False
        assert free.locked_sections == list(LOCKABLE_SECTIONS)
        assert free.queue == []
        assert free.do_not_do_yet == []
        assert free.contradictions == []
        assert free.diminishing_returns == []
        assert free.latest_change is None
        assert free.bottleneck.impact is None
        assert free.bottleneck.effort is None
        assert free.bottleneck.evidence == []

    def test_premium_gets_the_working(self, db):
        profile = _rich_profile(db)
        premium = strategy_service.project(strategy_service.compute(db, profile, now=NOW), is_premium=True)

        assert premium.is_premium is True
        assert premium.locked_sections == []
        assert premium.queue
        assert premium.bottleneck.impact in ("low", "medium", "high", "very_high")
        assert premium.bottleneck.effort in ("low", "medium", "high")
        assert premium.bottleneck.evidence
        assert premium.latest_change is not None

    def test_neither_projection_contains_a_hiring_probability(self, db):
        """Not "we don't populate it" -- there is no such field to populate, in either tier."""
        profile = _rich_profile(db)
        view = strategy_service.compute(db, profile, now=NOW)
        for is_premium in (True, False):
            payload = strategy_service.project(view, is_premium=is_premium).model_dump_json().lower()
            for banned in ("hiring_probability", "probability_of", "chance_of", "offer_probability",
                           "recruiter_probability", "percentage_chance"):
                assert banned not in payload

    def test_unknown_evidence_coverage_stays_unknown_rather_than_zero(self, db):
        profile = make_profile(db)
        free = strategy_service.project(strategy_service.compute(db, profile, now=NOW), is_premium=False)
        assert free.evidence_coverage_percent is None

    def test_only_series_with_real_history_are_reported(self, db):
        """A diminishing-returns section listing a dozen skills with no history answers nothing.
        Every row present has enough observations to support the verdict on it."""
        profile = _rich_profile(db)
        premium = strategy_service.project(strategy_service.compute(db, profile, now=NOW), is_premium=True)
        assert all(entry.is_sufficient for entry in premium.diminishing_returns)


class TestHistory:
    def test_history_lists_real_movements_newest_first(self, db):
        profile = _rich_profile(db)
        strategy_service.compute(db, profile, now=NOW)
        add_assessment_result(db, profile, {"Airflow": 0.95}, created_at=days_ago(1))
        strategy_service.compute(db, profile, now=NOW)

        history = strategy_service.get_history(db, profile.id)
        assert [row.sequence for row in history.snapshots] == [2, 1]

    def test_every_history_entry_explains_itself_in_one_request(self, db):
        """The history screen must render from a single call. Needing a /compare request per row
        would be a client-side N+1 over a list the server already holds in memory."""
        profile = _rich_profile(db)
        strategy_service.compute(db, profile, now=NOW)
        add_assessment_result(db, profile, {"Airflow": 0.95}, created_at=days_ago(1))
        strategy_service.compute(db, profile, now=NOW)

        history = strategy_service.get_history(db, profile.id)
        newest, oldest = history.snapshots
        assert newest.change_summary          # diffed against the entry below it
        assert oldest.change_summary          # the first-ever strategy still says so
        assert "first recorded strategy" in oldest.change_summary[0]

    def test_the_oldest_entry_on_a_page_is_still_explained(self, db):
        """Paging must not turn a real change into an unexplained row. The query deliberately
        fetches one extra snapshot purely as the baseline for the oldest entry shown."""
        profile = _rich_profile(db)
        strategy_service.compute(db, profile, now=NOW)
        add_assessment_result(db, profile, {"Airflow": 0.95}, created_at=days_ago(1))
        strategy_service.compute(db, profile, now=NOW)

        page = strategy_service.get_history(db, profile.id, limit=1)
        assert len(page.snapshots) == 1
        only = page.snapshots[0]
        assert only.sequence == 2
        # Off the bottom of the page there is a real predecessor, so this must NOT claim to be
        # the student's first-ever strategy.
        assert "first recorded strategy" not in " ".join(only.change_summary)

    def test_compare_reports_the_move_between_two_snapshots(self, db):
        profile = _rich_profile(db)
        strategy_service.compute(db, profile, now=NOW)
        add_assessment_result(db, profile, {"Airflow": 0.95}, created_at=days_ago(1))
        second = strategy_service.compute(db, profile, now=NOW)

        comparison = strategy_service.compare(db, profile.id, second.snapshot.id)
        assert comparison.compared_to is not None
        assert comparison.compared_to.sequence == 1
        assert comparison.change.summary

    def test_comparing_the_first_snapshot_admits_there_is_no_baseline(self, db):
        profile = _rich_profile(db)
        view = strategy_service.compute(db, profile, now=NOW)
        comparison = strategy_service.compare(db, profile.id, view.snapshot.id)
        assert comparison.compared_to is None
        assert comparison.change.changed is False

    def test_another_students_snapshot_is_not_found(self, db):
        """One error for "missing" and "not yours" -- distinguishing them would let anyone
        enumerate which snapshot ids exist."""
        mine = _rich_profile(db, user_id=1)
        theirs = _rich_profile(db, user_id=2)
        view = strategy_service.compute(db, mine, now=NOW)

        with pytest.raises(strategy_service.SnapshotNotFoundError):
            strategy_service.compare(db, theirs.id, view.snapshot.id)


class TestJobStrategy:
    def test_looking_at_a_job_does_not_write_strategy_history(self, db):
        """Reading a job advert is not a strategic event. Writing a snapshot for it would fill
        the timeline with rows the student did not cause."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, missing_skills=["Kafka"], matched_skills=["SQL"],
                                  listing_id_seed=99)
        before = db.query(PlacementStrategySnapshot).count()
        strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert db.query(PlacementStrategySnapshot).count() == before

    def test_unknown_skills_never_read_as_qualified(self, db):
        """The defect this test exists for: with no skill list recorded, job_hunt/provenance
        reports an advert's unmatched skills as UNCLEAR, so the missing-skill list comes back
        empty -- for exactly the same reason it is empty for a perfect candidate. Reading that
        as "has every skill" and recommending they apply would be this feature's worst possible
        failure, because skills are the heaviest readiness dimension and the first thing a
        recruiter screens on.
        """
        role = make_role(db, "Data Engineer X", {"SQL": 1.0})
        profile = make_profile(db, target_role_id=role.id, degree="B.Tech", branch="CSE",
                               graduation_year=2026, experience_level="fresher")
        add_assessment_result(db, profile, {"SQL": 0.95}, created_at=days_ago(2))
        add_resume(db, profile, skills=["sql"])
        add_interview_evaluation(db, profile, clarity=0.9, structure=0.9)
        # Deliberately no add_job_preferences: Job Hunt holds no skill list for this student.
        listing = add_tracked_job(db, profile, matched_skills=["SQL"], missing_skills=["Kafka"],
                                  score=0.9, components={"experience_fit": 0.9},
                                  listing_id_seed=66)

        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        assert result.decision == "INSUFFICIENT_INFORMATION"
        assert result.missing_skills == []          # not a gap -- an unknown
        assert result.unclear_skills == ["Kafka"]   # and it is named as one
        assert "Add your skills" in result.headline
        assert any("cannot tell whether you have" in reason for reason in result.reasons)

    def test_a_thin_profile_gets_insufficient_information_not_a_low_score(self, db):
        """The temptation is to render "prepare first" anyway, because it looks more useful.
        It is also a claim about a student we cannot actually make."""
        profile = make_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=[], missing_skills=[],
                                  listing_id_seed=77)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        assert result.decision == "INSUFFICIENT_INFORMATION"
        assert result.readiness_percent is None
        assert result.readiness_band is None
        assert result.unknown_dimensions

    def test_a_missing_skill_becomes_a_named_blocker(self, db):
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, missing_skills=["Kafka"], matched_skills=["SQL", "Python"],
                                  listing_id_seed=98)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        assert "Kafka" in result.missing_skills
        assert any(block.subject == "Kafka" and block.kind == "skill" for block in result.blockers)

    def test_the_match_score_is_the_match_engines_own(self, db):
        """No second scoring pass. If this disagreed with the Job Hunt screen about the same
        advert, the student would have no way to tell which was right."""
        from app.services.job_hunt import matching

        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=["SQL"], missing_skills=["Kafka"],
                                  score=0.71, listing_id_seed=97)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert result.score_version == matching.SCORE_VERSION

    def test_a_job_decision_never_advises_against_applying(self, db):
        """Adverts overstate requirements and the choice is the student's. The wording is about
        preparation, never about whether they deserve to apply."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, missing_skills=["Kafka", "Scala"], matched_skills=[],
                                  listing_id_seed=96)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        text = (result.headline + " " + " ".join(result.reasons)).lower()
        for discouraging in ("do not apply", "don't apply", "you are not good enough", "unqualified"):
            assert discouraging not in text

    def test_one_students_job_strategy_is_never_built_from_anothers_match(self, db):
        """Listings are shared rows and are deliberately readable by anyone -- a public job
        advert holds nothing private. The match is the part that is about a student, and it must
        be theirs. A student reading a listing another student has a stored match for gets their
        own assessment of it, recomputed, never the other student's.
        """
        owner = _rich_profile(db, user_id=1)
        listing = add_tracked_job(db, owner, missing_skills=["Kafka"], matched_skills=["SQL", "Python"],
                                  score=0.95, listing_id_seed=55)

        stranger = make_profile(db, user_id=2)
        theirs = strategy_service.get_job_strategy(db, stranger, listing.id, now=NOW)
        mine = strategy_service.get_job_strategy(db, owner, listing.id, now=NOW)

        # Same advert, genuinely different assessment -- not a copy of the owner's.
        assert theirs.verified_skill_count == 0
        assert mine.verified_skill_count > 0
        assert mine.match_score != theirs.match_score

    def test_an_unknown_listing_raises_rather_than_inventing_one(self, db):
        profile = _rich_profile(db)
        with pytest.raises(job_hunt_service.ListingNotFoundError):
            strategy_service.get_job_strategy(db, profile, 987654, now=NOW)

    def test_no_hiring_probability_on_a_job_decision(self, db):
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, missing_skills=["Kafka"], matched_skills=["SQL"],
                                  listing_id_seed=95)
        payload = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW).model_dump_json().lower()
        for banned in ("probability", "chance_of", "odds_of", "likelihood_of_hire"):
            assert banned not in payload


class TestJobDecisionOutcomes:
    """The four outcomes, and the line each one is not allowed to cross."""

    def _listing_for(self, db, profile, **kwargs):
        return add_tracked_job(db, profile, **kwargs)

    def test_a_close_match_with_one_gap_is_fix_before_applying(self, db):
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=["SQL", "Python"],
                                  missing_skills=["Kafka"], score=0.8,
                                  components={"experience_fit": 0.9}, listing_id_seed=201)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        assert result.decision == "FIX_BEFORE_APPLYING"
        assert "Kafka" in result.headline

    def test_the_headline_counts_gaps_accurately(self, db):
        """"the one thing" is a claim about how many things there are, so it has to be true."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=["SQL", "Python"],
                                  missing_skills=["Kafka", "Scala"], score=0.8,
                                  components={"experience_fit": 0.9}, listing_id_seed=202)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert "the one thing" not in result.headline
        assert "2 things" in result.headline

    def test_a_far_off_job_unrelated_to_the_bottleneck_is_not_a_current_priority(self, db):
        """Fixing this advert's gaps would not touch what is actually holding the student back,
        so saying "here is a to-do list for it" would quietly compete with their real work."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=[],
                                  missing_skills=["COBOL", "Mainframe", "JCL"], score=0.15,
                                  components={"experience_fit": 0.2}, listing_id_seed=203)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        assert result.decision == "NOT_A_CURRENT_PRIORITY"
        # It names what IS holding them back, so the card is redirecting rather than dismissing.
        assert result.headline

    def test_not_a_current_priority_never_tells_a_student_not_to_apply(self, db):
        """Adverts overstate requirements and the choice is always theirs."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=[],
                                  missing_skills=["COBOL", "Mainframe", "JCL"], score=0.15,
                                  components={"experience_fit": 0.2}, listing_id_seed=204)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        text = (result.headline + " " + " ".join(result.reasons)).lower()
        for discouraging in ("do not apply", "don't apply", "not qualified", "you will not get",
                             "waste of time", "unqualified"):
            assert discouraging not in text

    def test_without_a_career_bottleneck_nothing_is_called_off_the_critical_path(self, db):
        """"Not a priority" is a claim about the student's whole strategy. With no strategy to
        compare against there is no basis for it, so the decision falls back to the honest
        "there is work to do here"."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=[],
                                  missing_skills=["COBOL", "Mainframe"], score=0.15,
                                  components={"experience_fit": 0.2}, listing_id_seed=205)
        result = job_strategy.compose(db, profile, listing.id, career_decision=None, now=NOW)
        assert result.decision == "FIX_BEFORE_APPLYING"

    def test_every_decision_is_one_of_the_declared_outcomes(self, db):
        profile = _rich_profile(db)
        for seed, kwargs in enumerate(
            (
                dict(matched_skills=["SQL"], missing_skills=[], score=0.9),
                dict(matched_skills=["SQL"], missing_skills=["Kafka"], score=0.7),
                dict(matched_skills=[], missing_skills=["COBOL"], score=0.1),
                dict(matched_skills=[], missing_skills=[], score=0.5),
            ),
            start=210,
        ):
            listing = add_tracked_job(db, profile, listing_id_seed=seed, **kwargs)
            result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
            assert result.decision in job_strategy.JOB_DECISIONS

    def test_a_listing_that_names_no_skills_says_so_rather_than_guessing(self, db):
        """Never fabricate job requirements. An advert with nothing in it is an advert we cannot
        judge, and the response has to say which."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=[], missing_skills=[],
                                  score=0.5, listing_id_seed=220)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        assert any("does not state the skills" in reason for reason in result.reasons)

    def test_job_strategy_works_with_no_job_provider_configured(self, db, monkeypatch):
        """JOB_PROVIDER=none is the default and the shipping state until a key exists. Listings
        already in the database are still real data and must still be analysable -- the provider
        governs where *new* listings come from, not whether stored ones can be read."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "job_provider", "none")
        monkeypatch.setattr(settings, "jobvetta_api_key", None)

        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=["SQL"], missing_skills=["Kafka"],
                                  score=0.6, listing_id_seed=230)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        assert result.decision in job_strategy.JOB_DECISIONS
        assert result.listing_id == listing.id
        # Nothing about the advert was invented to fill the gap left by an absent provider.
        assert result.job_title == listing.title

    def test_nothing_about_a_job_is_fabricated_when_the_listing_is_sparse(self, db):
        """A listing with no salary, no skills and no component scores must produce absences,
        not plausible-looking defaults."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=[], missing_skills=[],
                                  score=0.0, components=None, listing_id_seed=231)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)

        assert result.decision == "INSUFFICIENT_INFORMATION"
        assert result.readiness_percent is None
        assert result.readiness_band is None
        assert result.missing_skills == []
        assert result.unclear_skills == []
        assert result.unknown_dimensions

    def test_every_blocker_has_a_real_destination(self, db):
        """No dead-end recommendations: each blocker names a place in the app that fixes it."""
        profile = _rich_profile(db)
        listing = add_tracked_job(db, profile, matched_skills=["SQL"], missing_skills=["Kafka"],
                                  score=0.6, listing_id_seed=221)
        result = strategy_service.get_job_strategy(db, profile, listing.id, now=NOW)
        known = {"roadmap", "practice", "project_builder", "skill_passport", "resume_ats",
                 "interview", "job_hunt", "tracker", "career_path", "profile", "career_strategy"}
        assert result.blockers
        for blocker in result.blockers:
            assert blocker.destination in known
