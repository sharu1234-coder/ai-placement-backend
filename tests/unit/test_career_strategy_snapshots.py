"""Strategy history: when a snapshot is written, and what a comparison is allowed to claim.

Two properties are worth more than the rest of this file.

The first is that a snapshot is written when the *strategy* changes and not when the *wording*
changes. Without that, opening the screen daily would fill history with rows claiming movement
that never happened, and "what changed this week?" would become unanswerable.

The second is that a comparison never overclaims. An item leaving a full queue may have been
completed or may have been outranked, and the two stored payloads genuinely cannot tell those
apart -- so the diff says "dropped out of your priorities", not "cleared". The tests below pin
that distinction, because it is exactly the kind of nuance a later refactor quietly flattens.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.placement_strategy_snapshot import PlacementStrategySnapshot
from app.services.career_strategy import bottleneck, snapshots
from app.services.career_strategy.bottleneck import (
    MAX_QUEUE_ITEMS,
    Bottleneck,
    DeferredItem,
    QueueItem,
    StrategyDecision,
)
from tests.fixtures.career_strategy_helpers import NOW, make_profile


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


def _bottleneck(**overrides) -> Bottleneck:
    defaults = dict(
        id="SKILL_GAP:SQL",
        category="SKILL_GAP",
        subject="SQL",
        subject_kind="skill",
        title="SQL",
        description="Nothing backs SQL yet.",
        severity="high",
        impact="very_high",
        effort="high",
        confidence="moderate",
        evidence=["SQL is a core skill for your target role."],
        recommended_action="Start learning SQL",
        destination="roadmap",
        reason_code="SKILL_GAP__SQL",
    )
    defaults.update(overrides)
    return Bottleneck(**defaults)


def _queue_item(item_id: str, **overrides) -> QueueItem:
    category, _, subject = item_id.partition(":")
    defaults = dict(
        id=item_id,
        category=category,
        subject=subject,
        title=f"Work on {subject}",
        impact="high",
        effort="low",
        destination="roadmap",
        reason=f"{subject} needs attention.",
        status="RECOMMENDED",
    )
    defaults.update(overrides)
    return QueueItem(**defaults)


def _decision(**overrides) -> StrategyDecision:
    defaults = dict(
        state="FOCUS_ON_SKILLS",
        primary=_bottleneck(),
        queue=[_queue_item("SKILL_GAP:SQL"), _queue_item("RESUME_GAP:Python")],
        do_not_do_yet=[],
        state_reason="One clear thing to work on.",
    )
    defaults.update(overrides)
    return StrategyDecision(**defaults)


class TestFingerprint:
    def test_the_same_decision_hashes_the_same_way(self):
        assert snapshots.fingerprint(_decision()) == snapshots.fingerprint(_decision())

    def test_rewording_an_explanation_is_not_a_change_of_strategy(self):
        """The single most important property in this module. Every free-text field is rendered
        from the same signals that produced the structure, so a copy-edit carries no new
        information -- and a history entry claiming the strategy moved because a sentence was
        reworded would be a lie told by a spelling fix."""
        original = _decision()
        reworded = _decision(
            primary=_bottleneck(
                description="A completely different sentence.",
                evidence=["Rewritten evidence line."],
            ),
            queue=[
                _queue_item("SKILL_GAP:SQL", reason="Different wording entirely."),
                _queue_item("RESUME_GAP:Python"),
            ],
            state_reason="Also reworded.",
        )
        assert snapshots.fingerprint(original) == snapshots.fingerprint(reworded)

    def test_a_different_bottleneck_is_a_change(self):
        moved = _decision(
            primary=_bottleneck(id="INTERVIEW_GAP:interview:clarity", category="INTERVIEW_GAP",
                                subject="interview:clarity", title="Interview clarity")
        )
        assert snapshots.fingerprint(_decision()) != snapshots.fingerprint(moved)

    def test_the_same_queue_in_a_different_order_is_a_change(self):
        """Order is advice. The same five items reordered is different guidance about what to
        do first, so it has to register as a change."""
        reordered = _decision(queue=[_queue_item("RESUME_GAP:Python"), _queue_item("SKILL_GAP:SQL")])
        assert snapshots.fingerprint(_decision()) != snapshots.fingerprint(reordered)

    def test_a_changed_impact_band_is_a_change(self):
        raised = _decision(queue=[_queue_item("SKILL_GAP:SQL", impact="low"), _queue_item("RESUME_GAP:Python")])
        assert snapshots.fingerprint(_decision()) != snapshots.fingerprint(raised)

    def test_a_changed_state_is_a_change(self):
        assert snapshots.fingerprint(_decision()) != snapshots.fingerprint(_decision(state="READY_TO_APPLY"))

    def test_a_changed_deferred_list_is_a_change(self):
        deferred = _decision(
            do_not_do_yet=[
                DeferredItem(id="STRENGTH:SQL", category=None, subject="SQL",
                             title="More practice on SQL", reason="Already strong.")
            ]
        )
        assert snapshots.fingerprint(_decision()) != snapshots.fingerprint(deferred)

    def test_an_engine_version_bump_is_a_change(self):
        """A decision that looks identical but was produced by different rules is not the same
        decision, and history must not pretend the engine has been silent."""
        assert snapshots.fingerprint(_decision()) != snapshots.fingerprint(_decision(strategy_version=99))


class TestRecording:
    def test_the_first_read_records_a_snapshot(self, db):
        profile = make_profile(db)
        row, created = snapshots.record(db, profile.id, _decision(), now=NOW)
        assert created is True
        assert row.sequence == 1
        assert row.state == "FOCUS_ON_SKILLS"
        assert row.primary_bottleneck_id == "SKILL_GAP:SQL"

    def test_opening_the_screen_again_does_not_record_anything(self, db):
        """The rule the spec states outright: persist on change, not on view."""
        profile = make_profile(db)
        snapshots.record(db, profile.id, _decision(), now=NOW)
        for _ in range(5):
            row, created = snapshots.record(db, profile.id, _decision(), now=NOW)
            assert created is False
        assert db.query(PlacementStrategySnapshot).count() == 1
        assert row.sequence == 1

    def test_a_real_change_records_a_second_snapshot(self, db):
        profile = make_profile(db)
        snapshots.record(db, profile.id, _decision(), now=NOW)
        moved = _decision(state="READY_TO_APPLY", primary=None, queue=[])
        row, created = snapshots.record(db, profile.id, moved, now=NOW)
        assert created is True
        assert row.sequence == 2
        assert row.primary_bottleneck_id is None
        assert db.query(PlacementStrategySnapshot).count() == 2

    def test_reverting_to_an_earlier_strategy_is_recorded_as_its_own_event(self, db):
        """A student who regresses has genuinely moved twice. Deduplicating against anything
        other than the immediately-previous snapshot would erase the second move and make the
        timeline claim they never came back."""
        profile = make_profile(db)
        snapshots.record(db, profile.id, _decision(), now=NOW)
        snapshots.record(db, profile.id, _decision(state="READY_TO_APPLY", primary=None, queue=[]), now=NOW)
        row, created = snapshots.record(db, profile.id, _decision(), now=NOW)
        assert created is True
        assert row.sequence == 3

    def test_the_stored_payload_is_the_whole_decision(self, db):
        """A summary would only ever answer the questions someone thought of on the day it was
        designed. Comparisons need the queue and the reasoning as they actually stood."""
        profile = make_profile(db)
        row, _created = snapshots.record(db, profile.id, _decision(), now=NOW)
        assert row.decision["primary"]["evidence"] == ["SQL is a core skill for your target role."]
        assert [item["id"] for item in row.decision["queue"]] == ["SKILL_GAP:SQL", "RESUME_GAP:Python"]

    def test_no_hiring_probability_reaches_storage(self, db):
        profile = make_profile(db)
        row, _created = snapshots.record(db, profile.id, _decision(), now=NOW)
        stored = str(row.decision).lower()
        for banned in ("probability", "chance_of", "odds", "likelihood_of_hire"):
            assert banned not in stored

    def test_snapshots_are_scoped_per_student(self, db):
        first = make_profile(db)
        second = make_profile(db, user_id=2)
        snapshots.record(db, first.id, _decision(), now=NOW)
        row, created = snapshots.record(db, second.id, _decision(), now=NOW)
        # Same decision, different student -- sequences are per profile, and one student's
        # history must never suppress another's write.
        assert created is True
        assert row.sequence == 1

    def test_get_refuses_another_students_snapshot(self, db):
        """Snapshot ids are sequential integers, and a strategy is the most personal thing this
        app stores. A bare primary-key lookup here would be an IDOR."""
        first = make_profile(db)
        second = make_profile(db, user_id=2)
        row, _created = snapshots.record(db, first.id, _decision(), now=NOW)
        assert snapshots.get(db, first.id, row.id) is not None
        assert snapshots.get(db, second.id, row.id) is None

    def test_history_is_newest_first_and_capped(self, db):
        profile = make_profile(db)
        for index in range(4):
            snapshots.record(db, profile.id, _decision(state=f"STATE_{index}"), now=NOW)
        rows = snapshots.history(db, profile.id, limit=2)
        assert [row.sequence for row in rows] == [4, 3]

    def test_history_limit_cannot_be_used_to_dump_the_table(self, db):
        profile = make_profile(db)
        snapshots.record(db, profile.id, _decision(), now=NOW)
        assert len(snapshots.history(db, profile.id, limit=10_000)) <= snapshots.MAX_HISTORY_LIMIT


class TestExplain:
    def test_the_first_snapshot_has_nothing_to_compare_against(self):
        """Comparing a first strategy against an assumed-empty one would report a student's
        entire starting position as though they had just acquired every problem in it."""
        change = snapshots.explain(None, _decision().model_dump(mode="json"))
        assert change.changed is False
        assert change.bottleneck_from is None
        assert "first recorded strategy" in change.summary[0]

    def test_an_unchanged_strategy_says_so_plainly(self):
        payload = _decision().model_dump(mode="json")
        change = snapshots.explain(payload, payload)
        assert change.changed is False
        assert change.summary == ["Nothing about your strategy has changed since the last snapshot."]

    def test_a_moved_bottleneck_names_both_ends(self):
        before = _decision().model_dump(mode="json")
        after = _decision(
            primary=_bottleneck(id="INTERVIEW_GAP:interview:clarity", category="INTERVIEW_GAP",
                                subject="interview:clarity", title="Interview clarity"),
        ).model_dump(mode="json")
        change = snapshots.explain(before, after)
        assert change.bottleneck_changed is True
        assert change.bottleneck_from.title == "SQL"
        assert change.bottleneck_to.title == "Interview clarity"
        assert "changed from SQL to Interview clarity" in " ".join(change.summary)

    def test_an_item_leaving_a_short_queue_is_genuinely_cleared(self):
        """Nothing could have displaced it: there was room to spare."""
        before = _decision().model_dump(mode="json")
        after = _decision(queue=[_queue_item("SKILL_GAP:SQL")]).model_dump(mode="json")
        change = snapshots.explain(before, after)
        assert [entry.id for entry in change.cleared] == ["RESUME_GAP:Python"]
        assert change.displaced == []
        assert any(line.startswith("Cleared:") for line in change.summary)

    def test_an_item_leaving_a_full_queue_is_not_claimed_as_finished(self):
        """The queue is capped, so a departure from a full queue is ambiguous -- possibly done,
        possibly outranked. Both payloads are silent on which, so the wording has to be too."""
        full = [_queue_item(f"SKILL_GAP:Skill{index}") for index in range(MAX_QUEUE_ITEMS)]
        before = _decision(queue=full).model_dump(mode="json")
        after = _decision(queue=full[:-1] + [_queue_item("RESUME_GAP:Python")]).model_dump(mode="json")
        change = snapshots.explain(before, after)
        assert change.cleared == []
        assert [entry.id for entry in change.displaced] == [f"SKILL_GAP:Skill{MAX_QUEUE_ITEMS - 1}"]
        line = next(entry for entry in change.summary if "dropped out" in entry)
        assert "may be resolved, or" in line

    def test_new_items_are_reported_and_surviving_ones_are_not_mistaken_for_new(self):
        before = _decision(queue=[_queue_item("SKILL_GAP:SQL")]).model_dump(mode="json")
        after = _decision(
            queue=[_queue_item("SKILL_GAP:SQL"), _queue_item("INTERVIEW_GAP:interview:clarity")]
        ).model_dump(mode="json")
        change = snapshots.explain(before, after)
        assert [entry.id for entry in change.added] == ["INTERVIEW_GAP:interview:clarity"]
        assert [entry.id for entry in change.still_open] == ["SKILL_GAP:SQL"]

    def test_a_state_change_is_reported(self):
        before = _decision().model_dump(mode="json")
        after = _decision(state="READY_TO_APPLY").model_dump(mode="json")
        change = snapshots.explain(before, after)
        assert change.state_changed is True
        assert change.state_from == "FOCUS_ON_SKILLS" and change.state_to == "READY_TO_APPLY"

    def test_a_comparison_across_an_engine_version_says_so(self):
        """Part of the difference is a change in the rules, not in the student. Presenting that
        as personal progress would be the most flattering possible lie."""
        before = _decision().model_dump(mode="json")
        after = _decision(strategy_version=99, state="READY_TO_APPLY").model_dump(mode="json")
        change = snapshots.explain(before, after)
        assert change.spans_version_change is True
        assert change.version_from == bottleneck.STRATEGY_VERSION and change.version_to == 99
        assert "different versions of the strategy engine" in change.summary[0]

    def test_a_comparison_never_claims_a_cause(self):
        """The payloads record what was decided, never why it moved. Any 'because you practised'
        line would be a plausible story with nothing behind it."""
        before = _decision().model_dump(mode="json")
        after = _decision(state="READY_TO_APPLY", primary=None, queue=[]).model_dump(mode="json")
        text = " ".join(snapshots.explain(before, after).summary).lower()
        for causal in ("because", "thanks to", "due to your", "caused by"):
            assert causal not in text

    def test_old_payloads_that_no_longer_match_the_model_are_still_readable(self):
        """History outlives the shape of the model that wrote it. A comparison that crashes on a
        decision recorded two engine versions ago is worse than one that reads it defensively."""
        # "BLOCKED" really was a state name under the v1 engine, which is exactly the kind of
        # payload this has to keep reading.
        ancient = {"state": "BLOCKED", "strategy_version": 0, "queue": [{"id": "OLD:Thing"}]}
        change = snapshots.explain(ancient, _decision().model_dump(mode="json"))
        assert change.state_from == "BLOCKED"
        assert [entry.id for entry in change.cleared] == ["OLD:Thing"]
        assert change.cleared[0].title == ""  # absent in the old payload, not invented here

    def test_sequence_and_timestamps_are_carried_through(self):
        earlier = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.timezone.utc)
        change = snapshots.explain(
            _decision().model_dump(mode="json"),
            _decision(state="READY_TO_APPLY").model_dump(mode="json"),
            from_sequence=3,
            to_sequence=4,
            from_created_at=earlier,
            to_created_at=NOW,
        )
        assert change.from_sequence == 3 and change.to_sequence == 4
        assert change.from_created_at == earlier and change.to_created_at == NOW
